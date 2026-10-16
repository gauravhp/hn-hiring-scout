import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor

st.set_page_config(page_title="HN Hiring Scout Dashboard", layout="wide")

DB_CONFIG = {
    "dbname": "hiring_scout",
    "user": "",
    "password": "",
    "host": "localhost",
    "port": 5432
}

def get_connection():
    return psycopg2.connect(**{k: v for k, v in DB_CONFIG.items() if v != ""})

def run_query(query, params=None):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchall()

def execute_commit(query, params=None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
        conn.commit()

# --- SIDEBAR CONTROLS ---
st.sidebar.title("Filters & Settings")

# 1. Company Ban Management
st.sidebar.subheader("Company Moderation")
banned_companies = run_query("SELECT id, name, ban_reason FROM companies WHERE is_banned = TRUE ORDER BY name ASC")
if banned_companies:
    with st.sidebar.expander(f"Banned Companies ({len(banned_companies)})"):
        for comp in banned_companies:
            col1, col2 = st.columns([3, 1])
            col1.write(f"**{comp['name']}** ({comp['ban_reason'] or 'No reason'})")
            if col2.button("Unban", key=f"unban_{comp['id']}"):
                execute_commit("UPDATE companies SET is_banned = FALSE, ban_reason = NULL WHERE id = %s", (comp["id"],))
                st.rerun()

show_banned = st.sidebar.checkbox("Include Banned Companies in List", value=False)

# 2. Report Selection
reports = run_query("SELECT id, report_title, source_file FROM reports ORDER BY id DESC")
report_opts = {r["id"]: f"{r['report_title']} ({r['source_file']})" for r in reports}
selected_report_ids = st.sidebar.multiselect("Filter by Report", options=list(report_opts.keys()), format_func=lambda x: report_opts[x])

# 3. Bucket and Score Filters
buckets = ["must_apply", "strong_maybe", "maybe", "weak_fit", "hard_filtered"]
selected_buckets = st.sidebar.multiselect("Recommendation Buckets", options=buckets, default=["strong_maybe", "maybe"])

min_score = st.sidebar.slider("Minimum Match Score", 0.0, 100.0, 50.0, step=5.0)
search_query = st.sidebar.text_input("Search (Stack, Title, Summary)", "")

# --- DATA QUERY ---
sql = """
    SELECT 
        j.id, j.role_title, j.score, j.bucket, j.location_remote, j.salary, j.tech_stack,
        j.apply_url, j.hn_comment_url, j.summary, j.why_it_fits, j.concerns, j.evidence_quotes,
        j.hard_filter_reasons, j.repost_count,
        c.id AS company_id, c.name AS company_name, c.is_banned, c.ban_reason,
        r.report_title
    FROM jobs j
    JOIN companies c ON j.company_id = c.id
    JOIN reports r ON j.last_seen_report_id = r.id
    WHERE 1=1
"""
params = []

if not show_banned:
    sql += " AND c.is_banned = FALSE"

if selected_report_ids:
    sql += " AND j.last_seen_report_id = ANY(%s)"
    params.append(selected_report_ids)

if selected_buckets:
    sql += " AND j.bucket = ANY(%s)"
    params.append(selected_buckets)

if min_score > 0:
    sql += " AND (j.score >= %s OR j.score IS NULL)"
    params.append(min_score)

if search_query:
    sql += """ AND (
        j.role_title ILIKE %s OR 
        c.name ILIKE %s OR 
        j.tech_stack ILIKE %s OR 
        j.summary ILIKE %s
    )"""
    q_param = f"%{search_query}%"
    params.extend([q_param, q_param, q_param, q_param])

sql += " ORDER BY j.score DESC NULLS LAST"

results = run_query(sql, params)

# --- DASHBOARD HEADER ---
st.title("HN Hiring Scout Report Dashboard")
st.caption("Centralized job candidate review, deduplication, and company governance.")

# Key metrics
m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Matches", len(results))
m2.metric("High Priority", len([r for r in results if r["bucket"] in ["must_apply", "strong_maybe"]]))
m3.metric("Banned Total", len(banned_companies))
m4.metric("Deduplicated Reposts", len([r for r in results if r["repost_count"] > 1]))

st.divider()

# --- JOB CARDS VIEW ---
if not results:
    st.info("No job opportunities found matching the selected criteria.")
else:
    for item in results:
        with st.container():
            col_main, col_action = st.columns([5, 1])
            
            with col_main:
                repost_badge = f" `Reposted {item['repost_count']}x`" if item['repost_count'] > 1 else ""
                banned_badge = " 🚫 `BANNED`" if item["is_banned"] else ""
                score_str = f"Score: **{item['score']}** | " if item['score'] is not None else ""
                
                st.markdown(f"### {item['company_name']} - {item['role_title']}{banned_badge}{repost_badge}")
                st.markdown(f"{score_str}**Bucket:** `{item['bucket']}` | **Location:** {item['location_remote'] or 'Unspecified'}")
                if item["salary"]:
                    st.markdown(f"**Compensation:** {item['salary']}")
                if item["tech_stack"]:
                    st.markdown(f"**Tech Stack:** `{item['tech_stack']}`")

            with col_action:
                if item["apply_url"] and item["apply_url"] != "Not found":
                    st.link_button("Apply", item["apply_url"], use_container_width=True)
                if item["hn_comment_url"]:
                    st.link_button("HN Thread", item["hn_comment_url"], use_container_width=True)
                
                if not item["is_banned"]:
                    with st.popover("Ban Company"):
                        ban_reason = st.text_input("Reason", placeholder="e.g., Consulting, Crypto, Bad fit", key=f"reason_{item['id']}")
                        if st.button("Confirm Ban", key=f"ban_btn_{item['id']}"):
                            execute_commit("UPDATE companies SET is_banned = TRUE, ban_reason = %s WHERE id = %s", (ban_reason, item["company_id"]))
                            st.rerun()

            with st.expander("Analysis & Evidence Quotes"):
                if item["summary"]:
                    st.markdown(f"**Summary:** {item['summary']}")
                if item["why_it_fits"]:
                    st.markdown("**Why it fits:**")
                    st.markdown(item["why_it_fits"])
                if item["concerns"]:
                    st.markdown("**Concerns:**")
                    st.markdown(item["concerns"])
                if item["hard_filter_reasons"]:
                    st.markdown(f"**Hard Filter Reasons:** {item['hard_filter_reasons']}")
                if item["evidence_quotes"]:
                    st.markdown("**Evidence Quotes:**")
                    st.info(item["evidence_quotes"])

            st.markdown("---")
