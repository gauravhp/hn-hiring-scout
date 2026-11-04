-- Add application-reporting fields to an existing hiring_scout database.
-- Run after connecting to the hiring_scout database:
--   psql -d hiring_scout -f 002_application_reporting.sql

BEGIN;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS applied_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS application_method VARCHAR(50),
    ADD COLUMN IF NOT EXISTS application_notes TEXT,
    ADD COLUMN IF NOT EXISTS skip_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_applied_at ON jobs(applied_at DESC);

COMMIT;
