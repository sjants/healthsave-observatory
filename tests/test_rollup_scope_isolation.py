"""Rollups must never mix aggregation scopes.

`normalization.fusion.can_sum` has stated the rule since the fusion core
landed — "two cumulative values may be added only if they are the same
component scope" — but nothing enforced it. Meanwhile
`_DEFAULT_SUMMARY_METRICS` (analysis.statistical.aggregator) deliberately
includes `activity.steps`, `activity.active_energy` and
`activity.exercise_minutes`, all `kind="daily_total"`.

Once a cumulative metric carries BOTH an all-source day total and its raw
components, they coexist as active rows in the same metric/day (different
dedup_key derivations, so no conflict, no error, no log line). An unfiltered
rollup then reports:

    avg   collapsing toward the per-sample mean (~40 kcal, not ~600)
    max   still the day total
    count inflated by the component fan-out (~200x)

and `delta_pct_vs_baseline` invents a -99% anomaly for the LLM narrator to
explain. These tests pin the scope selection that prevents it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalization.fusion import (  # noqa: E402
    AggregationScope,
    can_sum,
    daily_total_metric_ids,
    preferred_read_scope,
)
from storage.timescale.analysis import (  # noqa: E402
    fetch_canonical_coverage,
    fetch_metric_daily_series,
    summarize_metric_window,
)
from storage.timescale.observations import CanonicalObservationRepository  # noqa: E402

OWNER = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 9, 1, tzinfo=UTC)


class _FakeResult:
    def fetchone(self):
        return None

    def mappings(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter(())


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _FakeResult()


# ──────────────────────────────────────────────────────────────────
#  The rule and the scope it selects
# ──────────────────────────────────────────────────────────────────


def test_can_sum_still_states_the_rule_the_reads_now_enforce():
    """The predicate remains the canonical statement. The reads below enforce it
    structurally — by selecting ONE scope — which is strictly stronger than
    checking pairs after the fact."""
    assert can_sum(AggregationScope.INTERVAL_COMPONENT, AggregationScope.INTERVAL_COMPONENT)
    assert not can_sum(
        AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL, AggregationScope.INTERVAL_COMPONENT
    )
    assert not can_sum(
        AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL, AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL
    )


def test_daily_total_metric_ids_is_derived_from_the_registry_not_hand_listed():
    ids = daily_total_metric_ids()
    assert len(ids) == 64, "ontology v1 declares 64 kind='daily_total' metrics"
    assert "activity.steps" in ids
    assert "activity.active_energy" in ids
    assert "nutrition.water" in ids
    assert "vital.heart_rate" not in ids


@pytest.mark.parametrize(
    ("metric_id", "expected"),
    [
        ("activity.steps", AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL),
        ("activity.active_energy", AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL),
        ("vital.heart_rate", AggregationScope.INTERVAL_COMPONENT),
        ("vital.resting_heart_rate", AggregationScope.INTERVAL_COMPONENT),
        # An id the registry has never heard of must not resolve to a day total.
        ("not.a.metric", AggregationScope.INTERVAL_COMPONENT),
    ],
)
def test_preferred_read_scope_preserves_todays_numbers(metric_id, expected):
    assert preferred_read_scope(metric_id) is expected


# ──────────────────────────────────────────────────────────────────
#  Every rollup carries the predicate
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_metric_window_selects_one_scope():
    session = _FakeSession()
    await summarize_metric_window(
        session, "activity.active_energy", START, END, owner_id=OWNER, workspace_id=WORKSPACE
    )
    sql, params = session.calls[0]
    assert "aggregation_scope = :aggregation_scope" in sql, (
        "avg/min/max/count over a metric holding two scales is meaningless"
    )
    assert params["aggregation_scope"] == "owner_all_source_day_total"


@pytest.mark.asyncio
async def test_summarize_metric_window_uses_components_for_instant_metrics():
    session = _FakeSession()
    await summarize_metric_window(
        session, "vital.heart_rate", START, END, owner_id=OWNER, workspace_id=WORKSPACE
    )
    _sql, params = session.calls[0]
    assert params["aggregation_scope"] == "interval_component"


@pytest.mark.asyncio
async def test_daily_series_selects_one_scope():
    session = _FakeSession()
    await fetch_metric_daily_series(
        session, "activity.steps", START, END, owner_id=OWNER, workspace_id=WORKSPACE
    )
    sql, params = session.calls[0]
    assert "aggregation_scope = :aggregation_scope" in sql
    assert params["aggregation_scope"] == "owner_all_source_day_total"


@pytest.mark.asyncio
async def test_coverage_picks_the_right_scope_per_metric():
    """Coverage groups by metric_id, so one bind value cannot serve it — the
    per-metric CASE keeps every metric counted in the scope its analysis reads."""
    session = _FakeSession()
    await fetch_canonical_coverage(session, owner_id=OWNER, workspace_id=WORKSPACE)
    sql, params = session.calls[0]
    assert "aggregation_scope = CASE" in sql
    assert "metric_id = ANY(:daily_total_metric_ids)" in sql
    assert params["day_total_scope"] == "owner_all_source_day_total"
    assert params["component_scope"] == "interval_component"
    assert "activity.active_energy" in params["daily_total_metric_ids"]
    assert "vital.heart_rate" not in params["daily_total_metric_ids"]


@pytest.mark.asyncio
async def test_query_series_is_an_honest_transport_by_default():
    """The v2 series API must keep returning every scope, each row labelled —
    narrowing belongs to callers that aggregate, not to the transport."""
    session = _FakeSession()
    repo = CanonicalObservationRepository()
    await repo.query_series(
        session,
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        metric_id="activity.active_energy",
        start=START,
        end=END,
    )
    _sql, params = session.calls[0]
    assert params["aggregation_scope"] is None


@pytest.mark.asyncio
async def test_query_series_narrows_when_the_caller_will_aggregate():
    session = _FakeSession()
    repo = CanonicalObservationRepository()
    await repo.query_series(
        session,
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        metric_id="activity.active_energy",
        start=START,
        end=END,
        rollup_scope_only=True,
    )
    sql, params = session.calls[0]
    assert "aggregation_scope = CAST(:aggregation_scope AS text)" in sql
    assert params["aggregation_scope"] == "owner_all_source_day_total"


def test_fusion_update_cannot_demote_a_day_total():
    """`_UPDATE_VARIANTS_SQL` hard-writes 'interval_component' into its SET
    clause. Without a matching WHERE guard it would silently make an all-source
    day total summable — safe today only because its ids come from a guarded
    query."""
    from storage.timescale.fusion import _UPDATE_VARIANTS_SQL

    sql = str(_UPDATE_VARIANTS_SQL)
    assert sql.count("aggregation_scope = 'interval_component'") == 2, (
        "expected the scope in BOTH the SET clause and the WHERE guard"
    )
