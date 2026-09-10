-- 027: one-time cleanup of duplicate anomaly findings (issue #35).
--
-- Before #36 the rolling anomaly check compared each fresh anomaly's
-- `detected_at` (Python isoformat, '...+00:00') with the persisted one
-- (Pydantic JSON, '...Z') as raw strings. They never matched, so every run
-- re-persisted the same anomaly: one install had 74 copies of a single HRV
-- anomaly. #36 stops new copies; this removes the ones already written.
--
-- A duplicate is what the fixed engine treats as the same anomaly:
-- same owner/workspace, metric, direction, and detected_at INSTANT (compared
-- as timestamptz, so 'Z' and '+00:00' spellings match). The first copy (lowest
-- id) is kept. Rows whose detected_at is missing or unparseable are never
-- touched, and a bad value can never fail the migration.
--
-- Nothing has a foreign key to analysis_findings, but analysis_insights keeps
-- finding ids by value in `findings_used`; those are remapped to the kept copy
-- before the delete so no insight points at a removed row.
--
-- Idempotent: a second run finds no duplicates and changes nothing.

CREATE TEMP TABLE anomaly_finding_duplicates ON COMMIT DROP AS
WITH keyed AS (
    SELECT id,
           owner_id,
           workspace_id,
           COALESCE(metric, structured_data->>'metric') AS metric_key,
           structured_data->>'direction' AS direction_key,
           CASE
               WHEN pg_input_is_valid(structured_data->>'detected_at', 'timestamptz')
               THEN (structured_data->>'detected_at')::timestamptz
           END AS detected_instant
    FROM analysis_findings
    WHERE finding_type = 'anomaly'
),
ranked AS (
    SELECT id,
           first_value(id) OVER w AS keep_id,
           row_number() OVER w AS copy_number
    FROM keyed
    WHERE detected_instant IS NOT NULL
      AND metric_key IS NOT NULL
      AND direction_key IS NOT NULL
    WINDOW w AS (
        PARTITION BY owner_id, workspace_id, metric_key, direction_key, detected_instant
        ORDER BY id
    )
)
SELECT id AS duplicate_id, keep_id
FROM ranked
WHERE copy_number > 1;

UPDATE analysis_insights AS insight
SET findings_used = (
    SELECT array_agg(COALESCE(dup.keep_id, used.finding_id) ORDER BY used.position)
    FROM unnest(insight.findings_used) WITH ORDINALITY AS used(finding_id, position)
    LEFT JOIN anomaly_finding_duplicates AS dup ON dup.duplicate_id = used.finding_id
)
WHERE insight.findings_used && ARRAY(SELECT duplicate_id FROM anomaly_finding_duplicates);

DELETE FROM analysis_findings AS finding
USING anomaly_finding_duplicates AS dup
WHERE finding.id = dup.duplicate_id;
