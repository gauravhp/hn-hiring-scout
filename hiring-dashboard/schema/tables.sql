CREATE DATABASE hiring_scout;
\c hiring_scout;

-- 1. Companies Table with Banning Capability
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    clean_name VARCHAR(255) NOT NULL,
    is_banned BOOLEAN DEFAULT FALSE,
    ban_reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Reports Table to Track Report Metadata
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    report_title VARCHAR(255) NOT NULL,
    report_date TIMESTAMP WITH TIME ZONE,
    source_file VARCHAR(255) UNIQUE,
    thread_url TEXT,
    total_comments INT,
    analyzed_count INT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Jobs Table with Deduplication Fingerprinting
CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id) ON DELETE CASCADE,
    role_title VARCHAR(255) NOT NULL,
    fingerprint VARCHAR(64) UNIQUE NOT NULL, -- MD5 hash of (clean_company + clean_title)
    score NUMERIC(5, 2),
    bucket VARCHAR(50) NOT NULL,             -- must_apply, strong_maybe, maybe, weak_fit, hard_filtered
    location_remote TEXT,
    salary TEXT,
    tech_stack TEXT,
    hn_comment_url TEXT,
    apply_url TEXT,
    summary TEXT,
    why_it_fits TEXT,
    concerns TEXT,
    evidence_quotes TEXT,
    hard_filter_reasons TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,     -- Captures arbitrary future report fields
    first_seen_report_id INT REFERENCES reports(id),
    last_seen_report_id INT REFERENCES reports(id),
    repost_count INT DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for performant filtering
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_bucket ON jobs(bucket);
CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_companies_banned ON companies(is_banned);
CREATE INDEX IF NOT EXISTS idx_jobs_metadata ON jobs USING gin(metadata);
