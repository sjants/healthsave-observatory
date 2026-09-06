"""``samples[].aggregation`` — the wire declares its scope instead of being sniffed.

Before this field existed, the server's ONLY signal that a sample was an
all-source day total was a string match on the free-text ``source`` display
field ("HealthKit Statistics"). Change that label in the iOS extractor and
every day total silently reclassifies as a component; Android's raw records
(source "Pixel 9") happened to classify correctly for the wrong reason.

Both v2 payload models are ``extra='allow'``, so before this landed a client
could send ``aggregation`` and the server would silently ignore it — accepted,
no effect, no error. That is the worst failure mode available, and it is why
the server side had to land before any client emits the key.

DB-free: every test asserts on a 422 or on getting PAST validation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def client() -> TestClient:
    from server.main import app  # noqa: E402

    return TestClient(app, headers={"x-api-key": "test-contract"})


def _batch(sample: dict, metric: str = "active_energy_burned") -> dict:
    return {
        "schema_version": 2,
        "metric": metric,
        "batch_index": 0,
        "total_batches": 1,
        "samples": [sample],
        "source_bundle_id": "com.healthsave.ios",
    }


DAY_TOTAL = {
    "aggregation": "day_total",
    "localDate": "2026-08-30",
    "startDate": "2026-08-30T04:00:00.000Z",
    "endDate": "2026-08-31T04:00:00.000Z",
    "date": "2026-08-30T04:00:00.000Z",
    "qty": 612.0,
    "unit": "kcal",
    "tzOffsetMinutes": -240,
    "source": "HealthKit Statistics",
}

COMPONENT = {
    "aggregation": "component",
    "uuid": "D2C70000-0000-4000-8000-000000000101",
    "startDate": "2026-08-30T09:15:00.000Z",
    "endDate": "2026-08-30T09:16:00.000Z",
    "date": "2026-08-30T09:15:00.000Z",
    "qty": 41.2,
    "unit": "kcal",
    "tzOffsetMinutes": -240,
    "source": "Apple Watch",
}


def _rejects(client: TestClient, sample: dict, needle: str) -> None:
    resp = client.post("/api/v2/apple/batch", json=_batch(sample))
    assert resp.status_code == 422, resp.text
    assert needle in resp.text.lower(), resp.text


def _passes_validation(sample: dict) -> None:
    """Clears the contract gate.

    Asserted against the payload model rather than the route: this file is
    DB-free by design, and a valid body would otherwise run on into storage
    looking for a Timescale that isn't here. The model IS the gate — the route
    turns its ValidationError into the 422.
    """
    from server.api.v2_apple_batch import V2AppleBatchPayload  # noqa: E402

    V2AppleBatchPayload.model_validate(_batch(sample))


# ─── Vocabulary ───────────────────────────────────────────────────────


def test_unknown_aggregation_is_refused_not_guessed(client: TestClient) -> None:
    """Eric's principle: reject an ambiguous payload outright rather than guess
    at it. A near-miss of a real value must not fall through to inference."""
    _rejects(client, {**DAY_TOTAL, "aggregation": "daily"}, "aggregation")


def test_both_wire_scopes_clear_the_gate() -> None:
    _passes_validation(DAY_TOTAL)
    _passes_validation(COMPONENT)


def test_vendor_scopes_are_not_claimable_by_a_healthkit_client(client: TestClient) -> None:
    """The canonical enum has five scopes; only two have wire names. A client
    must not be able to assert a vendor-connector aggregate."""
    for vendor in (
        "device_day_total",
        "provider_account_day_total",
        "provider_reconciled_day_total",
    ):
        _rejects(client, {**DAY_TOTAL, "aggregation": vendor}, "aggregation")


# ─── day_total shape ──────────────────────────────────────────────────


def test_day_total_may_not_carry_hksample_identity(client: TestClient) -> None:
    """An HKStatistics bucket has no uuid by construction; its identity is
    (metric, localDate)."""
    _rejects(
        client,
        {**DAY_TOTAL, "uuid": "D2C70000-0000-4000-8000-000000000001"},
        "uuid",
    )


def test_day_total_requires_its_local_day(client: TestClient) -> None:
    sample = {k: v for k, v in DAY_TOTAL.items() if k != "localDate"}
    _rejects(client, sample, "localdate")


def test_day_total_requires_both_interval_bounds(client: TestClient) -> None:
    """The measurement window is the thing Eric lost on every interval-
    aggregated metric — a day total must state the window it covers."""
    sample = {k: v for k, v in DAY_TOTAL.items() if k != "endDate"}
    _rejects(client, sample, "day_total")


def test_day_total_local_date_must_agree_with_the_instant(client: TestClient) -> None:
    """A localDate that disagrees with startDate+tzOffsetMinutes means the
    client's notion of "which day is this" contradicts the instant it sent.
    Silently picking one corrupts every local-day rollup."""
    _rejects(client, {**DAY_TOTAL, "localDate": "2026-08-31"}, "disagrees")


def test_day_total_local_date_is_a_calendar_date(client: TestClient) -> None:
    _rejects(client, {**DAY_TOTAL, "localDate": "2026-08-30T04:00:00Z"}, "localdate")


def test_day_total_must_declare_its_unit(client: TestClient) -> None:
    sample = {k: v for k, v in DAY_TOTAL.items() if k != "unit"}
    _rejects(client, sample, "unit")


# ─── component shape ──────────────────────────────────────────────────


def test_component_must_carry_its_interval(client: TestClient) -> None:
    sample = {k: v for k, v in COMPONENT.items() if k not in ("startDate", "date")}
    _rejects(client, sample, "startdate")


def test_component_must_carry_identity(client: TestClient) -> None:
    sample = {k: v for k, v in COMPONENT.items() if k != "uuid"}
    _rejects(client, sample, "uuid")


# ─── backward compatibility ───────────────────────────────────────────


def test_legacy_aggregate_without_aggregation_still_ingests() -> None:
    """iOS <= 1.7.2 sends date-only aggregates with no `aggregation`, no unit
    and no interval. Those binaries are in the field; the shape must keep
    working forever, inferred by the legacy origin sniff."""
    _passes_validation(
        {"date": "2026-08-30T00:00:00.000Z", "qty": 8432, "source": "HealthKit Statistics"}
    )


def test_legacy_anchored_sample_without_aggregation_still_ingests() -> None:
    sample = {k: v for k, v in COMPONENT.items() if k != "aggregation"}
    _passes_validation(sample)
