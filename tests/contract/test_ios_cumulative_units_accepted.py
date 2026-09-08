"""Every cumulative metric's HealthKit unit spelling must be an allowed unit.

Cross-repo gate: reads ``ios_app/Sources/HealthSync/HealthTypes.swift`` directly
and self-skips when the sibling checkout isn't present (backend-only CI), like
the other cross-repo sync tests. It only truly runs from the workspace root via
``make trust-fast``.

Why it exists: ``allowed_units`` defaults to ``[canonical_unit]``
(contracts/ontology.py), so a daily-total metric accepts exactly ONE spelling
and none of the 64 declare alternates. Once iOS emits ``unit`` on day totals,
any HKUnit whose ``unitString`` differs from the ontology's canonical spelling
422s **every sample of that metric, deterministically, forever** — the exact
shape of wedge the 422-never-500 rule exists to avoid, except self-inflicted.

The audit was clean when the day-total wire landed (64/64). This keeps it that
way: adding a cumulative metric on either side with a mismatched unit is a red
build instead of a silently dead metric.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from normalization import apple_wire_metric  # noqa: E402

HEALTH_TYPES = REPO_ROOT.parent / "ios_app" / "Sources" / "HealthSync" / "HealthTypes.swift"

#: Swift unit expression -> the ``HKUnit.unitString`` it produces at runtime.
#: Apple's unitString values are stable API, so this table is a translation of
#: the Swift source, not a guess. An expression missing here fails the test
#: rather than being skipped — an unknown spelling is exactly the risk.
UNIT_STRING = {
    ".count()": "count",
    ".gram()": "g",
    ".hour()": "hr",
    ".kilocalorie()": "kcal",
    ".meter()": "m",
    ".minute()": "min",
    ".second()": "s",
    ".internationalUnit()": "IU",
    "HKUnit.internationalUnit()": "IU",
    "HKUnit.gramUnit(with: .milli)": "mg",
    "HKUnit.literUnit(with: .milli)": "mL",
    'HKUnit(from: "mcg")': "mcg",
}


def _ios_cumulative_units() -> dict[str, set[str]]:
    src = HEALTH_TYPES.read_text()
    block = re.search(
        r"static var cumulativeIdentifiers: Set<HKQuantityTypeIdentifier> \{(.*?)\n    \}",
        src,
        re.DOTALL,
    )
    assert block, "could not locate HealthTypes.cumulativeIdentifiers"
    cumulative = set(re.findall(r"\.([a-zA-Z0-9]+)", block.group(1)))

    triples = re.findall(
        r"\(\.([a-zA-Z0-9]+),\s*\"([a-z0-9_]+)\",\s*([^)]*\([^)]*\)[^,)]*|[^,)]+)\)", src
    )
    by_wire: dict[str, set[str]] = {}
    for identifier, wire, unit_expr in triples:
        if identifier in cumulative:
            by_wire.setdefault(wire, set()).add(unit_expr.strip())
    return by_wire


pytestmark = pytest.mark.skipif(
    not HEALTH_TYPES.exists(),
    reason="ios_app sibling checkout not present (backend-only CI)",
)


def test_ios_cumulative_metric_set_is_not_empty() -> None:
    units = _ios_cumulative_units()
    assert len(units) >= 60, f"expected the full cumulative family, parsed {len(units)}"


def test_every_cumulative_unit_expression_is_translatable() -> None:
    """An unmapped Swift expression means we cannot know what iOS will send."""
    unmapped = {
        (wire, expr)
        for wire, exprs in _ios_cumulative_units().items()
        for expr in exprs
        if expr not in UNIT_STRING
    }
    assert not unmapped, f"unmapped HKUnit expressions: {sorted(unmapped)}"


def test_every_cumulative_unit_is_in_the_metrics_allowed_units() -> None:
    offenders = []
    for wire, exprs in sorted(_ios_cumulative_units().items()):
        metric = apple_wire_metric(wire)
        if metric is None:
            offenders.append((wire, "no ontology metric for this wire name"))
            continue
        allowed = set(metric.allowed_units or [])
        for expr in exprs:
            spelling = UNIT_STRING.get(expr)
            if spelling is not None and spelling not in allowed:
                offenders.append((wire, f"{spelling!r} not in {sorted(allowed)}"))
    assert not offenders, (
        f"these metrics would 422 every sample once iOS declares their unit: {offenders}"
    )
