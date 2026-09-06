"""Parity manifest validity: ``contracts/parity.json`` is well-formed and honest.

The parity manifest is the cross-platform capability ledger: every wire metric
and user-facing feature carries an explicit per-platform availability decision
(``available`` / ``unavailable`` / ``planned``). The client test suites enforce
it from their side (iOS: set-equality against ``HealthTypes`` wire names;
Android: set-equality against the ``MetricCatalog``). This test enforces the
manifest's own honesty rules and runs everywhere — no sibling repo needed:

- ``planned`` requires ``planned_since`` (visible debt, never silent)
- ``unavailable`` requires a non-empty ``reason``
- every metric named by a request-corpus fixture exists in the manifest
- a metric whose canonical shape is a daily total declares the EMISSION SHAPE
  each platform actually sends, and a divergence between the two requires a
  ``shape_divergence_reason``

That last rule closes a blind spot. The manifest recorded only *whether* a
metric ships, never *what shape* it ships in — so ``active_energy_burned``
read ``{"ios": "available", "android": "available"}`` while iOS sent one
all-source daily total and Android sent raw per-record samples. Same metric id,
same server, two different meanings, and nothing could go red. A user syncing
both platforms into one owner double-counted energy, steps and distance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "contracts" / "parity.json"
ALLOWED = {"available", "unavailable", "planned"}
PLATFORMS = ("ios", "android")
#: Emission shapes a client can send. Flat strings on purpose: Android's
#: ParityManifestTest reads every value via ``jsonPrimitive.content``, so a
#: nested object or array would throw rather than fail cleanly.
SHAPES = {"instant", "interval", "component", "day_total", "component+day_total"}
CORPUS_DIRS = (
    REPO_ROOT / "tests" / "fixtures" / "apple_healthsave",
    REPO_ROOT / "tests" / "fixtures" / "android_healthsave",
)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _check_entry(kind: str, name: str, entry: dict) -> list[str]:
    problems: list[str] = []
    for platform in PLATFORMS:
        value = entry.get(platform)
        # Feature entries may use plain booleans for shipped platforms.
        if value is True or value is False:
            continue
        if value not in ALLOWED:
            problems.append(f"{kind} {name}: {platform}={value!r} not in {sorted(ALLOWED)}")
            continue
        if value == "planned" and not entry.get("planned_since"):
            problems.append(f"{kind} {name}: {platform}='planned' without planned_since")
        if value == "unavailable" and not entry.get("reason"):
            problems.append(f"{kind} {name}: {platform}='unavailable' without a reason")
    return problems


def _daily_total_wire_metrics() -> set[str]:
    """Wire metrics whose canonical shape is an all-source daily total.

    Derived from the ontology (``kind="daily_total"``) rather than hand-listed,
    so the rule cannot drift from the registry it describes.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from normalization import apple_wire_metric

    manifest = _manifest()
    out = set()
    for wire in manifest["metrics"]:
        metric = apple_wire_metric(wire)
        if metric is not None and metric.aggregation.kind == "daily_total":
            out.add(wire)
    return out


def _check_shapes(name: str, entry: dict) -> list[str]:
    problems: list[str] = []
    shapes: dict[str, str] = {}
    for platform in PLATFORMS:
        key = f"{platform}_shape"
        shape = entry.get(key)
        if shape is None:
            if entry.get(platform) == "available":
                problems.append(
                    f"metric {name}: {platform}='available' on a daily-total metric "
                    f"without {key} — availability alone hides a shape divergence"
                )
            continue
        if not isinstance(shape, str):
            problems.append(
                f"metric {name}: {key} must be a flat string, got {type(shape).__name__}"
            )
            continue
        if shape not in SHAPES:
            problems.append(f"metric {name}: {key}={shape!r} not in {sorted(SHAPES)}")
            continue
        shapes[platform] = shape

    if (
        len(shapes) == 2
        and len(set(shapes.values())) > 1
        and not entry.get("shape_divergence_reason")
    ):
        problems.append(
            f"metric {name}: ios_shape={shapes['ios']!r} and "
            f"android_shape={shapes['android']!r} differ without a "
            "shape_divergence_reason — a cross-platform semantic split is a "
            "recorded decision, not an omission"
        )
    return problems


def test_daily_total_metrics_declare_their_emission_shape() -> None:
    manifest = _manifest()
    daily_totals = _daily_total_wire_metrics()
    assert daily_totals, "no daily-total metrics resolved from the ontology"
    problems: list[str] = []
    for name in sorted(daily_totals):
        problems.extend(_check_shapes(name, manifest["metrics"][name]))
    assert not problems, "\n".join(problems)


def test_shape_fields_only_appear_where_they_mean_something() -> None:
    """A shape on a non-daily-total metric would be unenforced decoration."""
    manifest = _manifest()
    daily_totals = _daily_total_wire_metrics()
    stray = [
        name
        for name, entry in manifest["metrics"].items()
        if name not in daily_totals
        and any(k in entry for k in ("ios_shape", "android_shape", "shape_divergence_reason"))
    ]
    assert not stray, f"shape fields on non-daily-total metrics: {stray}"


def test_manifest_exists_and_versioned() -> None:
    manifest = _manifest()
    assert isinstance(manifest.get("manifest_version"), int)
    assert manifest.get("metrics"), "parity manifest has no metrics block"
    assert manifest.get("features"), "parity manifest has no features block"


def test_every_entry_is_honest() -> None:
    manifest = _manifest()
    problems: list[str] = []
    for name, entry in manifest["metrics"].items():
        problems += _check_entry("metric", name, entry)
    for name, entry in manifest["features"].items():
        problems += _check_entry("feature", name, entry)
    assert not problems, "parity manifest honesty violations:\n" + "\n".join(problems)


@pytest.mark.parametrize("corpus_dir", CORPUS_DIRS, ids=lambda p: p.name)
def test_every_corpus_metric_is_in_manifest(corpus_dir: Path) -> None:
    if not corpus_dir.exists() or not any(corpus_dir.glob("*.json")):
        pytest.skip(f"no request corpus at {corpus_dir.name} yet")
    metrics = set(_manifest()["metrics"])
    missing = {
        payload["metric"]
        for fixture in corpus_dir.glob("*.json")
        if isinstance(payload := json.loads(fixture.read_text()), dict) and "metric" in payload
    } - metrics
    assert not missing, (
        f"request corpus {corpus_dir.name} names metrics absent from "
        f"contracts/parity.json: {sorted(missing)}. Every wire metric is a "
        "recorded parity decision — add them to the manifest and re-mirror."
    )
