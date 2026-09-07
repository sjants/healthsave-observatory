"""``PUT /api/v2/sync/runs/{sync_run_id}/summary`` — a run that sent nothing still exists.

Receipts are written per HTTP batch. A HealthSave run that checked every
metric and found nothing new therefore left NO trace on the server, and
``GET /api/v2/sync/runs/latest`` kept answering with the previous run for as
long as that stayed true. From the Observatory an app syncing every 10 minutes
looked like it had stopped hours ago. Eric (2026-09-07) reported the client
half of the same blind spot (a no-op run rendered as "needs retry"); this
route is the server half: the client closes every completed run with one
small PUT, idempotent on the run id, carrying counts and metric NAMES only.

DB-free: every test asserts on a 422, or drives the route through a fake
repository. Storage-level merge behaviour lives in
``tests/test_api_contract.py`` (``latest_sync_run`` / ``sync_run``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from server.api import sync as sync_routes  # noqa: E402
from server.main import app  # noqa: E402

ROUTE = "/api/v2/sync/runs/ios-run-0001/summary"

ZERO_DELIVERY_BODY: dict[str, Any] = {
    "schema_version": 1,
    "outcome": "completed",
    "delivery": "none",
    "records_sent": 0,
    "metrics_checked": ["step_count", "heart_rate", "dietary_water"],
    "metrics_with_changes": [],
    "trigger": "observer",
    "intent": "latest_changes",
    "client_platform": "ios",
    "client_app_version": "1.8.0",
    "started_at": "2026-09-07T10:40:00.000Z",
    "completed_at": "2026-09-07T10:40:04.000Z",
}


class _RecordingRepo:
    """Stands in for the Timescale repository; captures what the route binds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_sync_run_summary(self, session: Any, **fields: Any) -> dict[str, Any]:
        self.calls.append(fields)
        return {
            "status": "ok",
            "sync_run_id": fields["sync_run_id"],
            "recorded": True,
            "received_at": "2026-09-07T10:40:05+00:00",
            "updated_at": "2026-09-07T10:40:05+00:00",
        }


class _Session:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _RecordingRepo, _Session]:
    repo = _RecordingRepo()
    session = _Session()
    monkeypatch.setattr(sync_routes, "_SYNC_RECEIPTS", repo)
    app.dependency_overrides[sync_routes.get_session] = lambda: session
    try:
        yield TestClient(app, headers={"x-api-key": "test-contract"}), repo, session
    finally:
        app.dependency_overrides.pop(sync_routes.get_session, None)


# ─── Surface ──────────────────────────────────────────────────────────


def test_route_is_registered_as_put() -> None:
    methods = {
        m
        for route in app.routes
        if getattr(route, "path", None) == "/api/v2/sync/runs/{sync_run_id}/summary"
        for m in (getattr(route, "methods", None) or set())
    }
    assert "PUT" in methods, "summary route must be a PUT (idempotent client retry)"


def test_setup_diagnostics_advertises_the_summary_route(client) -> None:
    tc, _, _ = client
    body = tc.get("/api/v2/setup/diagnostics").json()
    assert body["run_summary_endpoint"] == "/api/v2/sync/runs/{sync_run_id}/summary"


# ─── Happy path ───────────────────────────────────────────────────────


def test_zero_delivery_run_is_recorded_and_committed(client) -> None:
    tc, repo, session = client
    resp = tc.put(ROUTE, json=ZERO_DELIVERY_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sync_run_id"] == "ios-run-0001"
    assert body["recorded"] is True
    assert session.committed, "a recorded summary must be committed, not left in the tx"

    bound = repo.calls[0]
    assert bound["sync_run_id"] == "ios-run-0001"
    assert bound["outcome"] == "completed"
    assert bound["delivery"] == "none"
    assert bound["records_sent"] == 0
    assert bound["metrics_checked"] == ["step_count", "heart_rate", "dietary_water"]
    assert bound["metrics_with_changes"] == []
    assert bound["intent"] == "latest_changes"
    assert bound["client_platform"] == "ios"
    # Client clocks are stored as CLIENT times; the server stamps its own.
    assert bound["client_started_at"] is not None
    assert bound["client_completed_at"] is not None


def test_owner_defaults_to_sentinel_without_multi_user(client) -> None:
    from server.ingestion.owner import DEFAULT_OWNER_ID

    tc, repo, _ = client
    tc.put(ROUTE, json=ZERO_DELIVERY_BODY, headers={"x-user-id": "not-a-uuid"})
    # SECURITY-002: the header is ignored unless ALLOW_MULTI_USER is on.
    assert repo.calls[0]["owner_id"] == DEFAULT_OWNER_ID


def test_additive_unknown_keys_are_accepted(client) -> None:
    """A newer client may add keys; an older server must not 422 on them."""
    tc, _, _ = client
    resp = tc.put(ROUTE, json={**ZERO_DELIVERY_BODY, "future_key": {"nested": True}})
    assert resp.status_code == 200, resp.text


def test_summary_carries_names_and_counts_never_values() -> None:
    """The model has no field that could hold a health value.

    Privacy invariant: this route can only ever learn WHICH metrics were
    checked and HOW MANY records moved.
    """
    fields = set(sync_routes.SyncRunSummaryPayload.model_fields)
    for forbidden in ("samples", "qty", "value", "values", "records"):
        assert forbidden not in fields


# ─── Deterministic rejections (422, never 500) ───────────────────────


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome": "partial"},
        {"delivery": "carrier_pigeon"},
        {"intent": "everything"},
        {"records_sent": -1},
        {"schema_version": 2},
        {"metrics_checked": "step_count"},
        {"started_at": "yesterday"},
    ],
    ids=lambda m: next(iter(m)),
)
def test_bad_enumerations_and_types_are_422(client, mutation: dict[str, Any]) -> None:
    tc, repo, _ = client
    resp = tc.put(ROUTE, json={**ZERO_DELIVERY_BODY, **mutation})
    assert resp.status_code == 422, resp.text
    assert repo.calls == [], "a rejected body must never reach storage"


@pytest.mark.parametrize("missing", ["outcome", "delivery", "records_sent"])
def test_required_fields_are_422_when_absent(client, missing: str) -> None:
    tc, repo, _ = client
    body = {k: v for k, v in ZERO_DELIVERY_BODY.items() if k != missing}
    resp = tc.put(ROUTE, json=body)
    assert resp.status_code == 422, resp.text
    assert repo.calls == []


def test_blank_run_id_is_422(client) -> None:
    tc, repo, _ = client
    resp = tc.put("/api/v2/sync/runs/%20/summary", json=ZERO_DELIVERY_BODY)
    assert resp.status_code == 422, resp.text
    assert repo.calls == []


def test_summary_route_does_not_shadow_the_receipt_lookup(client) -> None:
    """``GET /api/v2/sync/runs/{id}`` must still resolve; the PUT lives one
    segment deeper and on a different method."""
    tc, _, _ = client
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.index("/api/v2/sync/runs/latest") < paths.index("/api/v2/sync/runs/{sync_run_id}")
    assert "/api/v2/sync/runs/{sync_run_id}/summary" in paths
