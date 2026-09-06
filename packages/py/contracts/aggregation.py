# SPDX-License-Identifier: Apache-2.0
"""What a cumulative value actually covers — the canonical vocabulary.

This lived in ``normalization.fusion`` while it was only a fusion concern, but
``aggregation_scope`` is a **contract**: it is a column on
``canonical_observations`` (migration 020), a field on every ``Observation``, a
``Literal`` in :mod:`contracts.data`, and a published union in the TypeScript
client. Four independent declarations of the same five strings, with nothing
binding them together, is how a value that is legal in Python reaches a SQL
CHECK constraint and returns a deterministic 500.

The enum lives here, at the bottom of the layering
(``contracts/ -> storage/ -> analysis/ -> apps/api``), so every zone can name it
without an import cycle. ``normalization.fusion`` re-exports it for the
behavioral helpers (``can_sum``, ``preferred_read_scope``,
``classify_wire_aggregation_scope``) that decide *with* it.
"""

from __future__ import annotations

from enum import StrEnum


class AggregationScope(StrEnum):
    """What a cumulative value actually covers. Values across scopes are NEVER
    summed and NEVER fused — a daily total is not its own 15-minute components,
    and a provider/all-source aggregate is not a single device's contribution."""

    INTERVAL_COMPONENT = "interval_component"
    DEVICE_DAY_TOTAL = "device_day_total"
    PROVIDER_ACCOUNT_DAY_TOTAL = "provider_account_day_total"
    PROVIDER_RECONCILED_DAY_TOTAL = "provider_reconciled_day_total"
    OWNER_ALL_SOURCE_DAY_TOTAL = "owner_all_source_day_total"
