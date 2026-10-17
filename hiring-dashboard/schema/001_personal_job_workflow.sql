-- Upgrade an existing hiring_scout database without losing company or job data.
-- Run after connecting to the hiring_scout database:
--   psql -d hiring_scout -f 001_personal_job_workflow.sql

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies' AND column_name = 'is_banned'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies' AND column_name = 'is_hidden'
    ) THEN
        ALTER TABLE companies RENAME COLUMN is_banned TO is_hidden;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies' AND column_name = 'ban_reason'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies' AND column_name = 'hide_reason'
    ) THEN
        ALTER TABLE companies RENAME COLUMN ban_reason TO hide_reason;
    END IF;
END $$;

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS hide_reason TEXT;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS application_status TEXT NOT NULL DEFAULT 'new',
    ADD COLUMN IF NOT EXISTS raw_apply_info TEXT,
    ADD COLUMN IF NOT EXISTS raw_hn_post_text TEXT;

-- Preserve the legacy report-derived Apply value for existing rows.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'jobs' AND column_name = 'apply_url'
    ) THEN
        UPDATE jobs
        SET raw_apply_info = apply_url
        WHERE raw_apply_info IS NULL
          AND apply_url IS NOT NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'jobs'::regclass
          AND conname = 'jobs_application_status_check'
    ) THEN
        ALTER TABLE jobs
            ADD CONSTRAINT jobs_application_status_check
            CHECK (application_status IN ('new', 'saved', 'applied', 'skipped'));
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'idx_companies_banned')
       AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'idx_companies_hidden') THEN
        ALTER INDEX idx_companies_banned RENAME TO idx_companies_hidden;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_jobs_application_status ON jobs(application_status);
CREATE INDEX IF NOT EXISTS idx_companies_hidden ON companies(is_hidden);

COMMIT;
