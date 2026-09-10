"""PostgreSQL proof for migration 027 (duplicate anomaly findings, issue #35).

The unit suite only inspects the migration text. This test executes the real
file against copies of ``analysis_findings`` / ``analysis_insights`` in an
isolated schema of the ephemeral E2E database, seeded with every case the
cleanup must get right, then runs it a second time to prove it is idempotent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from analysis.engine import _anomaly_key_from_data

DATABASE_URL = os.getenv("E2E_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/027_dedupe_anomaly_findings.sql"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set E2E_DATABASE_URL to run the migration proof (see `make e2e`)",
    ),
]

OWNER = UUID("00000000-0000-0000-0000-000000000001")
OTHER_OWNER = UUID("00000000-0000-0000-0000-000000000002")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000001")


def _anomaly(detected_at: str | None, *, metric: str = "hrv", direction: str = "up") -> dict:
    data = {"metric": metric, "magnitude": 3.1, "direction": direction, "severity": "alert"}
    if detected_at is not None:
        data["detected_at"] = detected_at
    return data


# (id, finding_type, metric column, owner, structured_data)
SEED = [
    (1, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T08:05:00Z")),  # first copy: kept
    (2, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T08:05:00Z")),  # repeat run: removed
    (3, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T08:05:00+00:00")),  # same instant
    (4, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T11:05:00+03:00")),  # same instant
    (5, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T08:05:00Z", direction="down")),
    (6, "anomaly", "heart_rate", OWNER, _anomaly("2026-09-05T08:05:00Z", metric="heart_rate")),
    (7, "anomaly", "hrv", OTHER_OWNER, _anomaly("2026-09-05T08:05:00Z")),
    (8, "anomaly", "hrv", OWNER, _anomaly("2026-09-05T08:35:00Z")),  # different instant
    (9, "anomaly", "hrv", OWNER, _anomaly("not-a-timestamp")),  # unparseable: untouched
    (10, "anomaly", "hrv", OWNER, _anomaly("not-a-timestamp")),
    (11, "anomaly", "hrv", OWNER, _anomaly(None)),  # missing: untouched
    (12, "summary", "hrv", OWNER, _anomaly("2026-09-05T08:05:00Z")),  # not an anomaly
    (13, "anomaly", None, OWNER, _anomaly("2026-09-05T08:05:00Z")),  # metric only in JSON
]
REMOVED = {2, 3, 4, 13}

# (id, findings_used) -> expected findings_used after the migration
INSIGHTS = [
    (1, [2, 5, 3], [1, 5, 1]),
    (2, [8, 9], [8, 9]),
    (3, None, None),
]


@pytest.mark.asyncio
async def test_migration_027_removes_only_later_copies_and_remaps_insights() -> None:
    schema = f"e2e_027_{uuid4().hex[:12]}"
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            "CREATE TABLE analysis_findings (LIKE public.analysis_findings INCLUDING DEFAULTS);"
            "CREATE TABLE analysis_insights (LIKE public.analysis_insights INCLUDING DEFAULTS);"
        )
        for finding_id, finding_type, metric, owner, data in SEED:
            await conn.execute(
                "INSERT INTO analysis_findings "
                "(id, finding_type, metric, owner_id, workspace_id, structured_data) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                finding_id,
                finding_type,
                metric,
                owner,
                WORKSPACE,
                json.dumps(data),
            )
        for insight_id, used, _ in INSIGHTS:
            await conn.execute(
                "INSERT INTO analysis_insights (id, insight_type, narrative, findings_used) "
                "VALUES ($1, 'daily', 'e2e', $2::bigint[])",
                insight_id,
                used,
            )

        migration_sql = MIGRATION.read_text()
        for _run in range(2):  # the second run must be a no-op
            async with conn.transaction():
                await conn.execute(migration_sql)

            remaining = await conn.fetch(
                "SELECT id, metric, owner_id, structured_data FROM analysis_findings ORDER BY id"
            )
            assert {row["id"] for row in remaining} == {row[0] for row in SEED} - REMOVED

            used_by_insight = {
                row["id"]: row["findings_used"]
                for row in await conn.fetch("SELECT id, findings_used FROM analysis_insights")
            }
            assert used_by_insight == {insight_id: expected for insight_id, _, expected in INSIGHTS}

        # The survivors are exactly what the fixed engine considers distinct:
        # no two parseable anomalies of one owner share an engine dedup key.
        keys = [
            _anomaly_key_from_data(row["metric"], json.loads(row["structured_data"]))
            for row in remaining
            if row["owner_id"] == OWNER and row["id"] not in {9, 10, 11, 12}
        ]
        assert len(keys) == len(set(keys))
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
