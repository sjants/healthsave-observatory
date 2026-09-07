-- 026_healthsave_sync_run_summaries.sql
--
-- A HealthSave sync run that had NOTHING to send left no trace on the server.
-- Receipts (007/011) are written per HTTP batch, so `GET /api/v2/sync/runs/latest`
-- kept answering with the PREVIOUS run for as long as the app found nothing new —
-- an app checking every 10 minutes looked, from the Observatory, like it had
-- stopped hours ago. Eric (2026-09-07) hit the client half of this (a no-op run
-- read as "needs retry"); this table is the server half.
--
-- The client now closes every completed run with ONE small
-- `PUT /api/v2/sync/runs/{sync_run_id}/summary` carrying what it checked and
-- what it sent. One row per run, idempotent on sync_run_id (the client may
-- retry). Receipts stay the delivery proof; this row is the run's existence
-- proof. `latest_sync_run` / `sync_run` merge the two.
--
-- Additive-only: new table, no existing object touched.

BEGIN;

CREATE TABLE IF NOT EXISTS healthsave_sync_run_summaries (
    sync_run_id           TEXT PRIMARY KEY,
    owner_id              UUID NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    client_platform       TEXT,
    client_app_version    TEXT,
    trigger               TEXT,
    intent                TEXT
        CHECK (intent IS NULL OR intent IN ('latest_changes', 'backfill', 'date_range')),
    outcome               TEXT NOT NULL
        CHECK (outcome IN ('completed', 'failed')),
    delivery              TEXT NOT NULL
        CHECK (delivery IN ('none', 'foreground', 'background_queued')),
    records_sent          INTEGER NOT NULL DEFAULT 0,
    metrics_checked       TEXT[] NOT NULL DEFAULT '{}',
    metrics_with_changes  TEXT[] NOT NULL DEFAULT '{}',
    error_class           TEXT,
    client_started_at     TIMESTAMPTZ,
    client_completed_at   TIMESTAMPTZ,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_healthsave_sync_run_summaries_received_at
    ON healthsave_sync_run_summaries (received_at DESC);

COMMIT;
