"""Cross-path fusion — the pure, deterministic core (vendor-connectors R5/R6).

Companion to :mod:`normalization.identity` (which derives stable Source/Device/
Stream UUIDs). This module is the referentially-transparent half of the decision
locked in ``docs_private/architecture/VENDOR_CONNECTORS.md``:

    The same physical device can reach the Observatory by TWO paths — relayed
    through Android Health Connect (often without a stable provider id) AND polled
    directly from the vendor cloud (strong provider ids). Both are kept as distinct
    streams; reads *fuse* them. The hard rule, from two independent GPT-5.5 Pro
    consults:

    **`semantic_key` is a recomputable fusion *assertion*, assigned AFTER provider
    + device identity is resolved — never a timestamp/value fingerprint computed at
    ingest.** A bare ``(metric, rounded_time, value)`` key eventually merges two
    genuine devices (Apple-Watch HR and a WHOOP band can both read 72 bpm at 10:00).

Two keys, two jobs:
- :func:`exact_ingest_key` — source-local idempotency (immutable; protects writes).
- :func:`semantic_key` — cross-path equivalence (nullable; assigned by matching).

No DB, no HTTP, no clock. Persistence (``canonical_observations`` columns, the
``fusion_decisions`` audit trail, ``device_identity_links``) is a later slice that
consumes these functions; keeping the rules pure makes the guardrails unit-testable
before any of that lands.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from uuid import UUID

from normalization.identity import normalize_origin


class AggregationScope(StrEnum):
    """What a cumulative value actually covers. Values across scopes are NEVER
    summed and NEVER fused — a daily total is not its own 15-minute components,
    and a provider/all-source aggregate is not a single device's contribution."""

    INTERVAL_COMPONENT = "interval_component"
    DEVICE_DAY_TOTAL = "device_day_total"
    PROVIDER_ACCOUNT_DAY_TOTAL = "provider_account_day_total"
    PROVIDER_RECONCILED_DAY_TOTAL = "provider_reconciled_day_total"
    OWNER_ALL_SOURCE_DAY_TOTAL = "owner_all_source_day_total"


class DeviceLinkConfidence(StrEnum):
    """Confidence that an HC stream and a direct stream are the same emitter.
    Manufacturer/model is evidence, not identity — only STRONG (or user-confirmed)
    may auto-link; never auto-link when two same-model devices are plausible."""

    NONE = "none"
    WEAK = "weak"  # package + manufacturer/model only
    MEDIUM = "medium"  # one active same-model device + longitudinal correlation
    STRONG = "strong"  # provider serial / HC stable device id / user-confirmed


def can_sum(a: AggregationScope, b: AggregationScope) -> bool:
    """Two cumulative values may be added only if they are the same component
    scope. Totals, account aggregates, and reconciled aggregates are terminal."""
    return a is b is AggregationScope.INTERVAL_COMPONENT


# ──────────────────────────────────────────────────────────────────
#  Wire aggregation → canonical scope
# ──────────────────────────────────────────────────────────────────

#: The literal ``source`` label HealthSave's cumulative extractor stamps on every
#: HKStatistics bucket (``HealthKitExtractor.fetchDailyStatistics``), and its
#: normalized key. Both live here rather than in the Apple normalizer because the
#: v1 projection in ``storage.timescale.measurements`` has to reach the SAME
#: verdict — two copies of this rule is how ``daily_activity`` and
#: ``canonical_observations`` ended up disagreeing about what a row means.
HEALTHKIT_STATISTICS_ORIGIN = "HealthKit Statistics"
HEALTHKIT_STATISTICS_ORIGIN_KEY = normalize_origin(HEALTHKIT_STATISTICS_ORIGIN)

#: The only two scopes a HealthKit client may assert on the wire. The remaining
#: ``AggregationScope`` members describe vendor connectors; a client must not be
#: able to claim them.
WIRE_AGGREGATION_SCOPES: dict[str, AggregationScope] = {
    "component": AggregationScope.INTERVAL_COMPONENT,
    "day_total": AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL,
}


#: Inverse of :data:`WIRE_AGGREGATION_SCOPES`. The canonical→wire direction is
#: needed by the v1 projection: it rebuilds writer-shaped samples from
#: ``canonical_observations`` (whose ``source`` has already been replaced by the
#: plugin id, so the legacy origin sniff is impossible there) and re-declares the
#: scope the normalizer already resolved. Scopes with no wire name are vendor
#: connector concerns and are simply not stamped.
WIRE_AGGREGATION_BY_SCOPE: dict[AggregationScope, str] = {
    scope: name for name, scope in WIRE_AGGREGATION_SCOPES.items()
}


class UnknownWireAggregation(ValueError):
    """A sample declared an ``aggregation`` the wire contract does not define.

    Deterministic and caller-visible on purpose: the ingest route turns this into
    a 422. Guessing a scope is exactly the ambiguity the v2 wire exists to remove.
    """


def classify_wire_aggregation_scope(
    *,
    declared: str | None,
    has_identity: bool,
    metric_is_daily_total: bool,
    origin_key: str,
) -> AggregationScope:
    """Resolve one wire sample's aggregation scope.

    Four tiers, most-authoritative first:

    1. ``declared`` — the explicit ``samples[].aggregation`` key (iOS 1.8.0+).
       An unrecognized value raises :class:`UnknownWireAggregation`; it is never
       guessed.
    2. HKSample identity present ⇒ a component. A statistics bucket has no
       ``uuid`` by construction, so identity is decisive.
    3. Legacy sniff for clients ≤ 1.7.2, which sent no ``aggregation``: a
       daily-total metric whose origin is the extractor's literal
       ``"HealthKit Statistics"`` label. Retained **permanently** — those
       binaries are in the field and must keep their current semantics.
    4. Anything else is a component. This is what makes Android's raw Health
       Connect records (origin ``"Pixel 9"``) classify correctly today.
    """
    if declared is not None:
        try:
            return WIRE_AGGREGATION_SCOPES[declared]
        except KeyError as exc:
            raise UnknownWireAggregation(declared) from exc
    if has_identity:
        return AggregationScope.INTERVAL_COMPONENT
    if metric_is_daily_total and origin_key == HEALTHKIT_STATISTICS_ORIGIN_KEY:
        return AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL
    return AggregationScope.INTERVAL_COMPONENT


def daily_total_metric_ids() -> frozenset[str]:
    """Ontology metric ids whose canonical shape is an all-source daily total.

    Computed from the registry rather than hand-listed so it cannot drift from
    ``kind="daily_total"``. 64 metrics as of ontology v1.
    """
    from contracts.ontology import REGISTRY

    return frozenset(
        metric_id
        for metric_id, definition in REGISTRY.items()
        if definition.aggregation.kind == "daily_total"
    )


def preferred_read_scope(metric_id: str) -> AggregationScope:
    """Which aggregation scope a ROLLUP over ``metric_id`` must select.

    Rollups (avg / sum / count over a window) are only meaningful within a single
    scope — see :func:`can_sum`. A daily-total metric that also carries raw
    components holds BOTH scales as active rows in the same metric/day, so an
    unfiltered ``avg`` collapses toward the per-sample mean while ``max`` stays the
    day total and ``count`` inflates by the component fan-out.

    The default preserves today's numbers exactly: daily-total metrics roll up
    over their all-source totals, everything else over interval components.
    """
    from contracts.ontology import REGISTRY

    definition = REGISTRY.get(metric_id)
    if definition is not None and definition.aggregation.kind == "daily_total":
        return AggregationScope.OWNER_ALL_SOURCE_DAY_TOTAL
    return AggregationScope.INTERVAL_COMPONENT


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def exact_ingest_key(
    owner_id: UUID,
    source_id: UUID,
    object_type: str,
    *,
    provider_object_id: str | None = None,
    fallback_fields: tuple[object, ...] = (),
) -> str:
    """Source-local idempotency key. Prefers the provider's stable object id; falls
    back to a composite of normalized fields. The provider *revision* timestamp is
    deliberately excluded — a revised object is the same ingest identity."""
    if provider_object_id:
        return "xik:v1:" + _digest(owner_id, source_id, object_type, provider_object_id)
    if not fallback_fields:
        raise ValueError("exact_ingest_key needs a provider_object_id or fallback_fields")
    return "xik:v1:" + _digest(owner_id, source_id, object_type, "composite", *fallback_fields)


def semantic_key(
    vendor_family: str,
    provider_subject_id: str | None,
    object_type: str,
    provider_object_id: str | None,
) -> str | None:
    """Provider-rooted equivalence anchor for a *direct* record. Returns ``None``
    when there is no strong provider identity (the normal Health-Connect-relayed
    case at ingest) — fusion fills it in later, it is never invented from time/value."""
    if not (provider_subject_id and provider_object_id):
        return None
    return f"sem:v1:{vendor_family}:{provider_subject_id}:{object_type}:{provider_object_id}"


@dataclass(frozen=True)
class SessionCandidate:
    """A workout/exercise/sleep session up for cross-path matching."""

    vendor_family: str
    activity_type: str
    start_epoch_s: float
    end_epoch_s: float
    provider_object_id: str | None  # present on direct records, absent via HC

    @property
    def duration_s(self) -> float:
        return self.end_epoch_s - self.start_epoch_s


@dataclass(frozen=True)
class FusionDecision:
    """The result of a match attempt — recorded verbatim in ``fusion_decisions``."""

    fuse: bool
    reason: str


# Conservative initial thresholds (VENDOR_CONNECTORS.md §3). Bias to NON-merge: a
# false split shows visible duplicates; a false merge silently destroys provenance.
_MAX_BOUNDARY_DRIFT_S = 5.0
_MIN_OVERLAP_RATIO = 0.98


def decide_session_fusion(
    direct: SessionCandidate,
    relayed: SessionCandidate,
    device_link: DeviceLinkConfidence,
) -> FusionDecision:
    """Decide whether a Health-Connect-relayed session is the same logical event as
    a direct vendor session. Identity-gated FIRST, then time/shape as corroboration —
    never the reverse. This is the Polar first-slice primitive."""
    if direct.provider_object_id is None:
        return FusionDecision(False, "no direct provider object id to anchor the match")
    if direct.vendor_family != relayed.vendor_family:
        return FusionDecision(False, "different vendor families")
    if device_link not in (DeviceLinkConfidence.STRONG, DeviceLinkConfidence.MEDIUM):
        return FusionDecision(False, f"device link too weak to fuse ({device_link.value})")
    if direct.activity_type != relayed.activity_type:
        return FusionDecision(False, "activity type mismatch")
    if abs(direct.start_epoch_s - relayed.start_epoch_s) > _MAX_BOUNDARY_DRIFT_S:
        return FusionDecision(False, "start times differ beyond tolerance")
    if abs(direct.end_epoch_s - relayed.end_epoch_s) > _MAX_BOUNDARY_DRIFT_S:
        return FusionDecision(False, "end times differ beyond tolerance")
    overlap = min(direct.end_epoch_s, relayed.end_epoch_s) - max(
        direct.start_epoch_s, relayed.start_epoch_s
    )
    span = max(direct.duration_s, relayed.duration_s)
    if span <= 0 or overlap / span < _MIN_OVERLAP_RATIO:
        return FusionDecision(False, "interval overlap below threshold")
    return FusionDecision(True, "vendor + device-link + activity + interval all agree")


# Primary-selection order for variants that ARE the same logical observation, at
# EQUAL granularity (VENDOR_CONNECTORS.md §3). Lower rank = preferred ("direct wins").
class VariantTier(IntEnum):
    DIRECT_WITH_PROVIDER_ID = 0
    DIRECT_WITH_DEVICE = 1
    HC_WITH_RECORD_UID = 2
    HC_PACKAGE_AND_DEVICE = 3
    HC_PACKAGE_ONLY = 4
    UNKNOWN = 5


def select_primary(tiers: list[VariantTier]) -> int | None:
    """Index of the primary variant (lowest tier). ``None`` for an empty list.

    Caller MUST only pass variants of equal semantic granularity — a device-specific
    HC reading must never lose to a direct *account aggregate*; those are different
    groups, not competing variants of one observation."""
    if not tiers:
        return None
    return min(range(len(tiers)), key=lambda i: tiers[i].value)
