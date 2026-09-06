"""The aggregation-scope vocabulary is declared in four places. They must agree.

Historically each declaration stood alone:

  1. ``AggregationScope``            — Python enum (was in normalization.fusion)
  2. ``SourceAggregationScope``      — Literal in contracts.data, for plugin manifests
  3. ``chk_canonical_obs_aggregation_scope`` — SQL CHECK, migration 020
  4. ``AggregationScope``            — union in the published TypeScript client

Nothing bound them. A value legal in Python but absent from the CHECK reaches
Postgres as an IntegrityError deep inside ``insert_many`` — a **deterministic
500**, which iOS treats as retryable forever and which therefore wedges the
metric permanently. The 422-never-500 rule exists precisely for this.

Item 1 is now the single source of truth (``contracts.aggregation``); this
module proves the other three still match it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from contracts.aggregation import AggregationScope  # noqa: E402
from contracts.data import SourceAggregationScope  # noqa: E402
from contracts.observation import Observation  # noqa: E402

CANONICAL = {scope.value for scope in AggregationScope}


def test_the_vocabulary_is_the_five_known_scopes():
    assert CANONICAL == {
        "interval_component",
        "device_day_total",
        "provider_account_day_total",
        "provider_reconciled_day_total",
        "owner_all_source_day_total",
    }


def test_contracts_data_literal_matches_the_enum():
    assert set(get_args(SourceAggregationScope)) == CANONICAL


def test_sql_check_constraint_matches_the_enum():
    """Migration 020's CHECK is the last line of defence. A Python-legal value
    missing here is a deterministic 500."""
    sql = (REPO_ROOT / "db/migrations/020_canonical_fusion_metadata.sql").read_text()
    match = re.search(
        r"chk_canonical_obs_aggregation_scope.*?aggregation_scope\s+IN\s*\((.*?)\)",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, "could not locate the aggregation_scope CHECK constraint"
    assert set(re.findall(r"'([a-z_]+)'", match.group(1))) == CANONICAL


def test_typescript_client_union_matches_the_enum():
    ts = (REPO_ROOT / "packages/ts/api-client/src/v2.ts").read_text()
    match = re.search(r"export type AggregationScope =(.*?);", ts, re.DOTALL)
    assert match, "could not locate the AggregationScope union in the TS client"
    assert set(re.findall(r'"([a-z_]+)"', match.group(1))) == CANONICAL


def test_observation_rejects_an_unknown_scope_in_pydantic_not_in_postgres():
    """THE reason this file exists. ``aggregation_scope`` was a bare ``str``
    with a string default, so an unknown value sailed through validation and
    only failed at the SQL CHECK — as a 500, on a route whose whole contract is
    'deterministic bad payloads return 422'."""
    with pytest.raises(ValidationError):
        Observation.model_validate({"aggregation_scope": "not_a_real_scope"})


def test_observation_default_is_unchanged():
    assert Observation.model_fields["aggregation_scope"].default is (
        AggregationScope.INTERVAL_COMPONENT
    )
