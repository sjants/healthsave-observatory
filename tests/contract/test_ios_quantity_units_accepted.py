"""Every quantity unit the iOS app sends must pass the v2 unit gate.

The iOS app reads each quantity metric in a fixed ``HKUnit`` and sends
``unit.unitString`` -- HealthKit's runtime spelling, which appears nowhere in
the Swift source (``HKUnit(from: "ml/kg*min")`` goes out as ``mL/min·kg``).
The ontology's ``allowed_units`` once pinned the source spelling instead, so
four metrics got a deterministic 422 on ``POST /api/v2/apple/batch`` (#37)
while every existing test stayed green: ``test_ios_cumulative_units_accepted``
covers cumulative metrics only and translates units with a hand table.

The catalog is owned by ios_app (``Fixtures/v2-ios-quantity-units.json``,
pinned to ``HealthTypes.quantityTypes`` by iOS ``V2RequestCorpusTests`` on a
real HealthKit). datahub mirrors it byte-equal (Law 3: change the owner, then
``cp``). The gate check reads the mirror, so it runs in backend-only CI too:
an ontology edit that drops a spelling the app sends goes red on the PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from server.api.v2_apple_batch import V2AppleBatchPayload

REPO_ROOT = Path(__file__).resolve().parents[2]
MIRROR = REPO_ROOT / "tests" / "fixtures" / "apple_healthsave_v2" / "v2-ios-quantity-units.json"
OWNER = (
    REPO_ROOT.parent
    / "ios_app"
    / "Tests"
    / "HealthSyncTests"
    / "Fixtures"
    / "v2-ios-quantity-units.json"
)

UNITS: dict[str, str] = json.loads(MIRROR.read_text(encoding="utf-8"))["units"]


def test_catalog_covers_the_ios_quantity_family() -> None:
    assert len(UNITS) >= 100, (
        f"expected the full HealthTypes.quantityTypes catalog, got {len(UNITS)}"
    )


@pytest.mark.skipif(
    not OWNER.exists(), reason="ios_app not checked out alongside datahub (backend-only CI)"
)
def test_mirror_matches_the_ios_owner_byte_for_byte() -> None:
    assert MIRROR.read_bytes() == OWNER.read_bytes(), (
        "datahub mirror drifted from ios_app; regenerate at the OWNER "
        "(ios_app V2RequestCorpusTests), then cp -- never edit the mirror"
    )


@pytest.mark.parametrize(("metric", "unit"), sorted(UNITS.items()))
def test_every_ios_quantity_unit_passes_the_v2_unit_gate(metric: str, unit: str) -> None:
    body = {
        "schema_version": 2,
        "metric": metric,
        "batch_index": 0,
        "total_batches": 1,
        "samples": [
            {
                "uuid": "d2c70000-0000-4000-8000-000000000099",
                "startDate": "2026-08-30T07:14:00-04:00",
                "endDate": "2026-08-30T07:14:00-04:00",
                "qty": 1,
                "unit": unit,
                "source": "Apple Watch",
            }
        ],
    }
    try:
        V2AppleBatchPayload.model_validate(body)
    except ValidationError as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"iOS sends {metric!r} in {unit!r} and the v2 gate rejects it: {exc}")
