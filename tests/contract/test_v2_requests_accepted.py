"""The v2 golden request corpus is not just pinned — it is INGESTIBLE.

``test_ios_v2_corpus_in_sync.py`` proves the fixtures equal the iOS app's v2
goldens; this module proves the server actually accepts them: every fixture
validates as ``V2AppleBatchPayload`` (the family-aware gate) and replays
through the live ``v2_apple_batch`` handler to a processed receipt carrying
``wire_schema_version: 2`` and the deletions block. If a server change starts
rejecting a real iOS family, it fails here instead of on the phone.

Runs WITHOUT the sibling repo (backend-only CI) — reads datahub's own mirror.
DB-free: the storage layer is the ``FakeSession`` double from
``tests/test_api_contract.py`` (extended with ``rowcount`` for the supersede
UPDATEs the v2 route issues).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from server.api.v2_apple_batch import V2AppleBatchPayload  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "apple_healthsave_v2"
FIXTURE_NAMES = sorted(p.name for p in FIXTURES_DIR.glob("*_batch.json"))

# Anchored families the iOS extractor emits through ``v2SampleBaseDict`` —
# every sample MUST carry the identity + interval + local-offset keys.
_ANCHORED_KEYS = {"uuid", "startDate", "endDate", "tzOffsetMinutes"}
# Date-bucketed aggregates are not HKSamples, so they carry no `uuid`. That is
# the ONLY v2 key they are exempt from. The previous blanket `continue` here
# excused them from `unit`, `startDate`/`endDate` and `tzOffsetMinutes` too —
# which turned a real limitation ("a statistics bucket has no HKSample
# identity") into a pinned requirement, and left ~60 cumulative metrics on the
# v1 shape inside a `schema_version: 2` envelope.
_AGGREGATE_FIXTURES = {"step_count_batch.json", "activity_summaries_batch.json"}

# Aggregates that declare `aggregation: "day_total"` and carry the local-midnight
# window, unit and offset.
_DAY_TOTAL_FIXTURES = {"step_count_batch.json", "activity_summaries_batch.json"}

# `activity_summaries` is a COMPOSITE day total: several daily totals in one
# dict, fanned out per wire metric by `_expand_activity_summary_sample`. One
# `unit` key cannot describe it, so its units travel as a map keyed by field.
# This is the family `active_energy_burned` actually arrives through.
_COMPOSITE_DAY_TOTAL_FIXTURES = {"activity_summaries_batch.json"}

# Aggregate families still on the pre-v2 shape. Empty by design — a new one
# must be a deliberate, named decision, not an omission that the aggregate
# exemption quietly absorbs.
_LEGACY_AGGREGATE_FIXTURES: set[str] = set()
# Raw HKSample COMPONENTS of a cumulative metric (iOS 1.8.0+, per-metric
# "Individual Samples" toggle). The same wire name also carries a day total, so
# every sample must DECLARE `aggregation: "component"` on top of the anchored
# identity set — the server must never infer it from the presence of a uuid.
_COMPONENT_FIXTURES = {"dietary_water_batch.json"}
# Category events carry ``qty`` as a duration in seconds with no unit key —
# the ontology has no quantity definition for them, so the unit gate is
# lenient by design (matches the extractor).
_QTY_WITHOUT_UNIT_FIXTURES = {"mindful_session_batch.json"}


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _v2_headers(name: str, payload: dict) -> dict[str, str]:
    """Identity + idempotency headers the way the iOS v2 wire sends them
    (``contracts/v2-ios-headers.json`` — the v1 set plus the advisory
    ``X-HealthSave-Schema-Version``)."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    payload_hash = "sha256:" + hashlib.sha256(body).hexdigest()
    return {
        "Idempotency-Key": payload_hash,
        "X-HealthSave-Payload-Hash": payload_hash,
        "X-HealthSave-Sync-Run-ID": f"ios-v2-corpus-run-{name}",
        "X-HealthSave-Batch-ID": f"ios-v2-corpus-batch-{name}",
        "X-HealthSave-Metric": payload["metric"],
        "X-HealthSave-Batch-Index": str(payload["batch_index"]),
        "X-HealthSave-Total-Batches": str(payload["total_batches"]),
        "X-HealthSave-Schema-Version": str(payload["schema_version"]),
    }


def test_v2_corpus_is_not_empty() -> None:
    assert FIXTURE_NAMES, "v2 corpus is empty"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_declares_schema_version_two(name: str) -> None:
    raw = _load(name)
    assert raw["schema_version"] == 2, f"{name}: v2 corpus must declare schema_version=2"
    assert raw["source_bundle_id"] == "com.healthsave.ios"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_validates_as_v2_payload(name: str) -> None:
    """The family-aware gate accepts every real iOS emission family."""
    payload = V2AppleBatchPayload.model_validate(_load(name))
    assert payload.samples, f"{name} has no samples"
    for sample in payload.samples:
        dumped = sample.model_dump(exclude_none=True)
        if name in _COMPONENT_FIXTURES:
            assert dumped.get("aggregation") == "component", (
                f"{name}: a cumulative metric's raw sample must DECLARE it is a component — "
                "the same wire name also carries day totals: {dumped}"
            )
            assert "localDate" not in dumped, f"{name}: only day totals name a calendar day"
            assert _ANCHORED_KEYS <= set(dumped), f"{name}: component missing identity keys: {dumped}"
        if name in _AGGREGATE_FIXTURES:
            assert "uuid" not in dumped, f"{name}: aggregates carry no HKSample identity"
            assert "date" in dumped
            if name in _DAY_TOTAL_FIXTURES:
                assert dumped.get("aggregation") == "day_total", (
                    f"{name}: a day total must DECLARE its scope — the server "
                    "must never have to infer it from the source label"
                )
                for key in ("localDate", "startDate", "endDate", "tzOffsetMinutes"):
                    assert key in dumped, f"{name}: day total missing {key}: {dumped}"
                if name in _COMPOSITE_DAY_TOTAL_FIXTURES:
                    # Every measured field declares its own unit, so a consumer
                    # never has to know activeEnergyBurned is kcal while
                    # appleExerciseTime is minutes.
                    units = dumped.get("units")
                    assert isinstance(units, dict) and units, (
                        f"{name}: a composite day total must carry a per-field units map"
                    )
                    measured = {
                        key
                        for key, value in dumped.items()
                        if isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and key != "tzOffsetMinutes"
                    }
                    missing_units = measured - set(units)
                    assert not missing_units, (
                        f"{name}: no unit declared for {sorted(missing_units)}"
                    )
                elif "qty" in dumped:
                    assert dumped.get("unit"), f"{name}: day total must declare its unit"
                assert dumped.get("date") == dumped["startDate"], (
                    f"{name}: day total lost the v1 date key"
                )
            else:
                assert name in _LEGACY_AGGREGATE_FIXTURES, (
                    f"{name}: a new aggregate family must either declare "
                    "`aggregation` or be recorded as a known legacy shape"
                )
            continue
        missing = _ANCHORED_KEYS - set(dumped)
        assert not missing, f"{name}: anchored sample missing v2 keys {sorted(missing)}: {dumped}"
        if "qty" in dumped:
            if name not in _QTY_WITHOUT_UNIT_FIXTURES:
                assert dumped.get("unit"), f"{name}: quantity sample must declare its unit"
            # v1 superset: the same body must still key its time on ``date``
            # for the 404/405 fallback path and third-party v1 consumers.
            assert dumped.get("date") == dumped["startDate"], (
                f"{name}: quantity sample lost the v1 date key"
            )


def test_percent_family_is_per_hundred_on_the_v2_wire() -> None:
    """Eric's 0.94-vs-94 case: with ``unit: "%"`` declared, the value is UCUM
    per-hundred, never HealthKit's fraction."""
    sample = _load("oxygen_saturation_batch.json")["samples"][0]
    assert sample["unit"] == "%"
    assert 50 <= sample["qty"] <= 100, sample


def test_heart_rate_fixture_carries_motion_context_and_deletions() -> None:
    raw = _load("heart_rate_batch.json")
    assert {s["motionContext"] for s in raw["samples"]} == {"sedentary", "active"}
    assert raw["deletions"], "heart_rate golden must exercise the deletions array"


def test_resting_heart_rate_fixture_has_a_real_interval() -> None:
    """RHR is interval-aggregated; the end bound is the revision identity."""
    sample = _load("resting_heart_rate_batch.json")["samples"][0]
    assert sample["startDate"] != sample["endDate"]


def _v2_fake_session():
    """``FakeSession`` plus ``rowcount`` on the supersede UPDATEs the v2 route
    issues (``mark_canonical_observations_superseded`` /
    ``mark_v1_dedicated_superseded`` read ``result.rowcount``)."""
    from tests.test_api_contract import FakeResult, FakeSession

    class _RowcountResult(FakeResult):
        def __init__(self, rowcount: int):
            super().__init__()
            self.rowcount = rowcount

    class _Session(FakeSession):
        async def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if sql.startswith("UPDATE") and "'superseded'" in sql:
                self.calls.append((sql, params or {}))
                return _RowcountResult(rowcount=len((params or {}).get("uuids") or []))
            return await super().execute(statement, params)

    return _Session()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_fixture_replays_to_processed_v2_receipt(name: str) -> None:
    """Every corpus request goes through the LIVE v2 handler to a receipt."""
    from server.api.v2_apple_batch import v2_apple_batch

    from tests.test_api_contract import FakeRequest

    payload = _load(name)
    headers = _v2_headers(name, payload)
    session = _v2_fake_session()

    result = await v2_apple_batch(FakeRequest(payload, headers=headers), None, session)

    assert result["status"] == "processed", f"{name}: not processed: {result}"
    assert result["wire_schema_version"] == 2, f"{name}: response must echo the wire version"
    assert result["metric"] == payload["metric"]
    assert result["records_received"] == len(payload["samples"])
    assert result["records_rejected"] == 0, f"{name}: server rejected v2 corpus samples: {result}"
    assert result["records_accepted"] >= 1, f"{name}: nothing accepted: {result}"
    deletions = payload.get("deletions", [])
    assert result["deletions"]["received"] == len(deletions), result["deletions"]
    if deletions:
        assert result["deletions"]["canonical_superseded"] == len(deletions), result["deletions"]
    receipt = session.insert_params_for("healthsave_sync_receipts")
    assert receipt is not None, f"{name}: no sync receipt row recorded"
    assert receipt["sync_run_id"] == headers["X-HealthSave-Sync-Run-ID"]
