import streamlit as st
import psycopg2
from psycopg2.extras import RealDictCursor

st.set_page_config(page_title="HN Hiring Scout Dashboard", layout="wide")

DB_CONFIG = {
    "dbname": "hiring_scout",
    "user": "",
    "password": "",
    "host": "localhost",
    "port": 5432,
}
APPLICATION_STATUSES = ["new", "saved", "applied", "skipped"]
STATUS_ACTIONS = [("saved", "Save for Later"), ("applied", "Applied"), ("skipped", "Skip")]


def get_connection():
    return psycopg2.connect(**{key: value for key, value in DB_CONFIG.items() if value != ""})


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


def status_label(status):
    return status.replace("_", " ").title()


# --- SIDEBAR CONTROLS ---
st.sidebar.title("Filters & Settings")
hidden_companies = run_query(
    "SELECT id, name, hide_reason FROM companies WHERE is_hidden = TRUE ORDER BY name ASC"
)
if hidden_companies:
    with st.sidebar.expander(f"Hidden Companies ({len(hidden_companies)})"):
        for company in hidden_companies:
            name_column, action_column = st.columns([3, 1])
            name_column.write(f"**{company['name']}** ({company['hide_reason'] or 'No reason'})")
            if action_column.button("Unhide", key=f"unhide_{company['id']}"):
                execute_commit(
                    "UPDATE companies SET is_hidden = FALSE, hide_reason = NULL WHERE id = %s",
                    (company["id"],),
                )
                st.rerun()

include_hidden = st.sidebar.checkbox("Include Hidden Companies in List", value=False)

reports = run_query("SELECT id, report_title, source_file FROM reports ORDER BY id DESC")
report_options = {report["id"]: f"{report['report_title']} ({report['source_file']})" for report in reports}
selected_report_ids = st.sidebar.multiselect(
    "Filter by Report",
    options=list(report_options.keys()),
    format_func=lambda report_id: report_options[report_id],
)

buckets = ["must_apply", "strong_maybe", "maybe", "weak_fit", "hard_filtered"]
selected_buckets = st.sidebar.multiselect(
    "Recommendation Buckets", options=buckets, default=["strong_maybe", "maybe"]
)
selected_statuses = st.sidebar.multiselect(
    "Application Status",
    options=APPLICATION_STATUSES,
    default=APPLICATION_STATUSES,
    format_func=status_label,
)
min_score = st.sidebar.slider("Minimum Match Score", 0.0, 100.0, 50.0, step=5.0)
search_query = st.sidebar.text_input("Search (Stack, Title, Summary)", "")

# --- DATA QUERY ---
sql = """
    SELECT 
        j.id, j.role_title, j.score, j.bucket, j.application_status,
        j.location_remote, j.salary, j.tech_stack, j.raw_apply_info,
        j.raw_hn_post_text, j.hn_comment_url, j.summary, j.why_it_fits,
        j.concerns, j.evidence_quotes, j.hard_filter_reasons, j.repost_count,
        c.id AS company_id, c.name AS company_name, c.is_hidden, c.hide_reason,
        r.report_title
    FROM jobs j
    JOIN companies c ON j.company_id = c.id
    JOIN reports r ON j.last_seen_report_id = r.id
    WHERE 1=1
"""
params = []

if not include_hidden:
    sql += " AND c.is_hidden = FALSE"
if selected_report_ids:
    sql += " AND j.last_seen_report_id = ANY(%s)"
    params.append(selected_report_ids)
if selected_buckets:
    sql += " AND j.bucket = ANY(%s)"
    params.append(selected_buckets)
if selected_statuses:
    sql += " AND j.application_status = ANY(%s)"
    params.append(selected_statuses)
else:
    sql += " AND FALSE"
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
    query_param = f"%{search_query}%"
    params.extend([query_param, query_param, query_param, query_param])

sql += " ORDER BY j.score DESC NULLS LAST"
results = run_query(sql, params)

# --- DASHBOARD HEADER ---
st.title("HN Hiring Scout")
st.caption("A personal workspace for reviewing and tracking job applications.")
metric_total, metric_priority, metric_hidden, metric_reposts = st.columns(4)
metric_total.metric("Matches in View", len(results))
metric_priority.metric(
    "High Priority",
    len([result for result in results if result["bucket"] in ["must_apply", "strong_maybe"]]),
)
metric_hidden.metric("Hidden Companies", len(hidden_companies))
metric_reposts.metric("Deduplicated Reposts", len([result for result in results if result["repost_count"] > 1]))
st.divider()

# --- JOB CARDS VIEW ---
if not results:
    st.info("No job opportunities found matching the selected filters.")
else:
    for item in results:
        with st.container():
            repost_badge = f" · Reposted {item['repost_count']}x" if item["repost_count"] > 1 else ""
            hidden_badge = " · Hidden" if item["is_hidden"] else ""
            score_text = f"Score: **{item['score']}** · " if item["score"] is not None else ""

            st.markdown(f"### {item['company_name']} — {item['role_title']}")
            st.caption(
                f"{score_text}Bucket: {item['bucket']} · Status: {status_label(item['application_status'])}"
                f"{hidden_badge}{repost_badge}"
            )
            st.markdown(f"**Location:** {item['location_remote'] or 'Unspecified'}")
            if item["salary"]:
                st.markdown(f"**Compensation:** {item['salary']}")
            if item["tech_stack"]:
                st.markdown(f"**Tech Stack:** {item['tech_stack']}")

            st.markdown("**Apply:**")
            st.code(item["raw_apply_info"] or "Not provided in the report", language=None)

            action_columns = st.columns([1.4, 1, 1, 1.1, 1])
            for column, (status, label) in zip(action_columns[:3], STATUS_ACTIONS):
                if column.button(
                    label,
                    key=f"status_{status}_{item['id']}",
                    type="primary" if item["application_status"] == status else "secondary",
                    use_container_width=True,
                ):
                    execute_commit(
                        "UPDATE jobs SET application_status = %s, updated_at = NOW() WHERE id = %s",
                        (status, item["id"]),
                    )
                    st.rerun()

            with action_columns[3]:
                hide_label = "Unhide Company" if item["is_hidden"] else "Hide Company"
                with st.popover(hide_label, use_container_width=True):
                    if item["is_hidden"]:
                        if st.button("Unhide Company", key=f"card_unhide_{item['company_id']}"):
                            execute_commit(
                                "UPDATE companies SET is_hidden = FALSE, hide_reason = NULL WHERE id = %s",
                                (item["company_id"],),
                            )
                            st.rerun()
                    else:
                        hide_reason = st.text_input(
                            "Reason (optional)",
                            placeholder="e.g., not a fit",
                            key=f"hide_reason_{item['company_id']}",
                        )
                        if st.button("Hide Company", key=f"hide_{item['company_id']}"):
                            execute_commit(
                                "UPDATE companies SET is_hidden = TRUE, hide_reason = %s WHERE id = %s",
                                (hide_reason or None, item["company_id"]),
                            )
                            st.rerun()

            with action_columns[4]:
                if item["hn_comment_url"]:
                    st.link_button("HN Thread", item["hn_comment_url"], use_container_width=True)

            st.markdown("**Raw HN post**")
            st.code(item["raw_hn_post_text"] or "Not available for this legacy job record.", language=None)

            with st.expander("Analysis & Evidence"):
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

            st.divider()
