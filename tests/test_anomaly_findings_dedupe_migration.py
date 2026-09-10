from pathlib import Path

from db.migrate import _migration_sql

MIGRATION = Path(__file__).resolve().parents[1] / "db/migrations/027_dedupe_anomaly_findings.sql"


def test_anomaly_findings_dedupe_is_scoped_safe_and_keeps_the_first_copy() -> None:
    assert MIGRATION.exists(), "issue #35 duplicates need a tracked production cleanup"
    sql = MIGRATION.read_text()
    upper_sql = sql.upper()

    # Only anomaly findings, keyed exactly like the fixed engine dedup key
    # (metric, direction, detected_at instant), scoped per owner/workspace.
    assert "WHERE finding_type = 'anomaly'" in sql
    assert "COALESCE(metric, structured_data->>'metric')" in sql
    assert "structured_data->>'direction'" in sql
    assert "PARTITION BY owner_id, workspace_id, metric_key, direction_key, detected_instant" in sql

    # Instants, not strings: 'Z' and '+00:00' must collapse. An unparseable
    # value is skipped via pg_input_is_valid, never cast (a cast error would
    # fail the migrate service and block every upgrade).
    assert "pg_input_is_valid(structured_data->>'detected_at', 'timestamptz')" in sql
    assert "detected_instant IS NOT NULL" in sql

    # The first copy survives; only later copies are removed.
    assert "ORDER BY id" in sql
    assert "copy_number > 1" in sql

    # By-value references in analysis_insights are remapped before the delete.
    assert upper_sql.index("UPDATE ANALYSIS_INSIGHTS") < upper_sql.index(
        "DELETE FROM ANALYSIS_FINDINGS"
    )

    # Data cleanup only: no schema changes (the temp table's ON COMMIT DROP
    # is the one allowed DROP).
    for forbidden in ("DROP TABLE", "DROP INDEX", "DROP COLUMN", "ALTER TABLE", "TRUNCATE"):
        assert forbidden not in upper_sql

    runner_sql = _migration_sql(MIGRATION)
    assert not runner_sql.upper().startswith("BEGIN;")
    assert "ON COMMIT DROP" in runner_sql
