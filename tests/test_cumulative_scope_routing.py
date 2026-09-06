"""Cumulative metrics project to the v1 legacy tables BY AGGREGATION SCOPE.

``daily_activity`` holds one row per ``(date, device_id, owner_id)`` and its
upsert is ``{column} = EXCLUDED.{column}`` — **last write wins, it does not
sum** (``storage.timescale.measurements._ingest_daily_quantity``). Before this
routing existed, every sample for a metric in ``DAILY_ACTIVITY_QUANTITY_FIELDS``
went there regardless of what it meant, so:

  * raw per-sample components would leave ``active_calories`` equal to the LAST
    sample's few kcal, and the Grafana activity dashboard
    (``deploy/grafana/dashboards/activity.json`` — ``sum(active_calories)``)
    would report that as the day;
  * Android's raw Health Connect records (origin ``"Pixel 9"``) were already
    being written this way, even though the canonical normalizer had always
    classified them ``interval_component``.

The rule: only an all-source HealthKit day total projects to ``daily_activity``.
Everything else preserves every row in ``quantity_samples``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from normalization.fusion import (  # noqa: E402
    WIRE_AGGREGATION_BY_SCOPE,
    AggregationScope,
    UnknownWireAggregation,
    classify_wire_aggregation_scope,
)

from tests.test_api_contract import FakeRequest, FakeSession  # noqa: E402

HK_STATS_ORIGIN = "healthkit statistics"


# ──────────────────────────────────────────────────────────────────
#  The classifier itself — the single rule both sides share
# ──────────────────────────────────────────────────────────────────


def test_declared_aggregation_wins_over_every_heuristic():
    """Tier 1. An explicit declaration is authoritative even when the legacy
    origin sniff would have said the opposite."""
    assert (
        classify_wire_aggregation_scope(
            declared="component",
            has_identity=False,
            metric_is_daily_total=True,
            origin_key=HK_STATS_ORIGIN,
        )
        is AggregationScope.INTERVAL_COMPONENT
    )
    assert (
        classify_wire_aggregation_scope(
            declared="day_total",
            has_identity=True,
            metric_is_daily_total=False,
            origin_key="apple watch",
        )
        is AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL
    )


def test_unknown_declared_aggregation_raises_rather_than_guessing():
    """Ambiguity is refused, never resolved by assumption — the whole reason the
    v2 wire declares this in the first place."""
    with pytest.raises(UnknownWireAggregation):
        classify_wire_aggregation_scope(
            declared="daily",  # near-miss of "day_total"
            has_identity=False,
            metric_is_daily_total=True,
            origin_key=HK_STATS_ORIGIN,
        )


def test_hksample_identity_means_component():
    """Tier 2. A statistics bucket has no uuid by construction, so identity is
    decisive even for a daily-total metric."""
    assert (
        classify_wire_aggregation_scope(
            declared=None,
            has_identity=True,
            metric_is_daily_total=True,
            origin_key=HK_STATS_ORIGIN,
        )
        is AggregationScope.INTERVAL_COMPONENT
    )


def test_legacy_origin_sniff_still_classifies_pre_1_8_clients():
    """Tier 3. iOS <= 1.7.2 sends no ``aggregation``; those binaries are in the
    field and must keep their current semantics permanently."""
    assert (
        classify_wire_aggregation_scope(
            declared=None,
            has_identity=False,
            metric_is_daily_total=True,
            origin_key=HK_STATS_ORIGIN,
        )
        is AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL
    )


def test_named_device_origin_is_a_component_not_a_day_total():
    """Tier 4. Android's raw Health Connect records, and any per-source Apple
    contribution, are components — never an all-source total."""
    for origin in ("pixel 9", "apple watch", "unknown"):
        assert (
            classify_wire_aggregation_scope(
                declared=None,
                has_identity=False,
                metric_is_daily_total=True,
                origin_key=origin,
            )
            is AggregationScope.INTERVAL_COMPONENT
        ), origin


def test_wire_vocabulary_round_trips_and_excludes_vendor_scopes():
    """A HealthKit client may assert exactly two scopes. The vendor-connector
    scopes have no wire name so a client cannot claim them."""
    assert WIRE_AGGREGATION_BY_SCOPE[AggregationScope.INTERVAL_COMPONENT] == "component"
    assert WIRE_AGGREGATION_BY_SCOPE[AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL] == "day_total"
    for vendor_scope in (
        AggregationScope.DEVICE_DAY_TOTAL,
        AggregationScope.PROVIDER_ACCOUNT_DAY_TOTAL,
        AggregationScope.PROVIDER_RECONCILED_DAY_TOTAL,
    ):
        assert vendor_scope not in WIRE_AGGREGATION_BY_SCOPE


# ──────────────────────────────────────────────────────────────────
#  End-to-end routing through the real ingest route
# ──────────────────────────────────────────────────────────────────


async def _ingest(metric: str, sample: dict) -> FakeSession:
    session = FakeSession()
    await server.apple_batch(FakeRequest({"metric": metric, "samples": [sample]}), session)
    return session


@pytest.mark.asyncio
async def test_day_total_projects_to_daily_activity_only():
    session = await _ingest(
        "step_count",
        {"date": "2026-04-10T00:00:00+00:00", "qty": 8432, "source": "HealthKit Statistics"},
    )
    daily = session.insert_params_for("daily_activity")
    assert daily is not None, "an all-source day total must reach daily_activity"
    assert daily["steps"] == 8432
    assert session.insert_params_for("quantity_samples") is None


@pytest.mark.asyncio
async def test_component_never_reaches_daily_activity():
    """THE regression guard. ``daily_activity`` is last-write-wins, so a
    component landing there silently replaces the day's real total."""
    session = await _ingest(
        "active_energy_burned",
        {
            "date": "2026-04-10T09:15:00+00:00",
            "qty": 41.2,
            "unit": "kcal",
            "source": "Apple Watch",
            "uuid": "D2C70000-0000-4000-8000-000000000101",
            "aggregation": "component",
        },
    )
    assert session.insert_params_for("daily_activity") is None, (
        "a component must NEVER project to daily_activity — its upsert is "
        "last-write-wins, so this would overwrite the day's real total"
    )
    quantity = session.insert_params_for("quantity_samples")
    assert quantity is not None, "components must be preserved in quantity_samples"
    assert quantity["metric_name"] == "active_energy_burned"


@pytest.mark.asyncio
async def test_android_style_raw_record_is_treated_as_a_component():
    """Android sends raw ``ActiveCaloriesBurnedRecord`` rows with a device
    origin and no ``aggregation``. The canonical store already called these
    components; the legacy projection now agrees instead of flattening them
    into one last-write-wins day row."""
    session = await _ingest(
        "step_count",
        {"date": "2026-04-10T09:15:00+00:00", "qty": 137, "source": "Pixel 9"},
    )
    assert session.insert_params_for("daily_activity") is None
    assert session.insert_params_for("quantity_samples") is not None


@pytest.mark.asyncio
async def test_mixed_batch_splits_both_ways():
    """Both scopes in one batch: the total projects, the component is preserved,
    and neither is lost."""
    session = FakeSession()
    await server.apple_batch(
        FakeRequest(
            {
                "metric": "step_count",
                "samples": [
                    {
                        "date": "2026-04-10T00:00:00+00:00",
                        "qty": 8432,
                        "source": "HealthKit Statistics",
                        "aggregation": "day_total",
                    },
                    {
                        "date": "2026-04-10T09:15:00+00:00",
                        "qty": 137,
                        "source": "Apple Watch",
                        "uuid": "D2C70000-0000-4000-8000-000000000102",
                        "aggregation": "component",
                    },
                ],
            }
        ),
        session,
    )
    daily = session.insert_params_for("daily_activity")
    assert daily is not None and daily["steps"] == 8432
    assert session.insert_params_for("quantity_samples") is not None
