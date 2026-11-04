import html
import json
import re
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="HN Hiring Scout Dashboard", layout="wide")

DB_CONFIG = {
    "dbname": "hiring_scout",
    "user": "",
    "password": "",
    "host": "localhost",
    "port": 5432,
}
APPLICATION_STATUSES = ["new", "saved", "applied", "skipped"]
REPORT_APPLICATION_STATUSES = ["saved", "applied", "skipped"]
APPLICATION_METHODS = ["", "Email", "Application form", "Referral", "Other"]
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.+-])")
URL_PATTERN = re.compile(r"https?://[^\s,]+")
EMPTY_FILTER_VALUE = "__none__"


def get_connection():
    return psycopg2.connect(
        **{key: value for key, value in DB_CONFIG.items() if value != ""}
    )


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


def application_contacts(raw_apply_info):
    """Return the first email and URL exactly as present in the raw report field."""
    if not raw_apply_info:
        return None, None
    email_match = EMAIL_PATTERN.search(raw_apply_info)
    url_match = URL_PATTERN.search(raw_apply_info)
    return (
        email_match.group(0) if email_match else None,
        url_match.group(0) if url_match else None,
    )


def inferred_application_method(raw_apply_info):
    email, application_url = application_contacts(raw_apply_info)
    if email and not application_url:
        return "Email"
    if application_url:
        return "Application form"
    return "Other"


def render_copyable_email(email):
    escaped_email = html.escape(email)
    email_json = json.dumps(email)
    components.html(
        f"""
        <style>
            body {{ margin: 0; font-family: sans-serif; }}
            .apply-email {{ display: flex; align-items: center; gap: 0.45rem; }}
            button {{
                border: 0; background: transparent; cursor: pointer; font-size: 1.1rem;
                padding: 0.1rem 0.25rem;
            }}
            button:hover {{ background: #f0f2f6; border-radius: 0.25rem; }}
        </style>
        <div class="apply-email">
            <button id="copy-email" title="Copy email" aria-label="Copy email">📋</button>
            <code>{escaped_email}</code>
        </div>
        <script>
            const email = {email_json};
            document.getElementById("copy-email").addEventListener("click", async () => {{
                try {{
                    await navigator.clipboard.writeText(email);
                }} catch (_) {{
                    const textarea = document.createElement("textarea");
                    textarea.value = email;
                    document.body.appendChild(textarea);
                    textarea.select();
                    document.execCommand("copy");
                    textarea.remove();
                }}
            }});
        </script>
        """,
        height=34,
    )


def query_values(name):
    return st.query_params.get_all(name)


def initial_multiselect(name, options, default):
    values = query_values(name)
    if not values:
        return default
    if values == [EMPTY_FILTER_VALUE]:
        return []
    return [value for value in values if value in options]


def initial_score():
    try:
        return min(100.0, max(0.0, float(st.query_params.get("min_score", "50"))))
    except (TypeError, ValueError):
        return 50.0


def persist_filters(
    include_hidden,
    report_ids,
    selected_buckets,
    selected_statuses,
    min_score,
    search_query,
):
    st.query_params.clear()
    st.query_params["include_hidden"] = "1" if include_hidden else "0"
    st.query_params["report"] = [str(report_id) for report_id in report_ids] or [
        EMPTY_FILTER_VALUE
    ]
    st.query_params["bucket"] = selected_buckets or [EMPTY_FILTER_VALUE]
    st.query_params["status"] = selected_statuses or [EMPTY_FILTER_VALUE]
    st.query_params["min_score"] = str(min_score)
    if search_query:
        st.query_params["search"] = search_query


def render_top_header():
    st.title("HN Hiring Scout")
    st.divider()


def review_navigation_params():
    """Return current review filters, preferring live widget state over the URL."""
    params = {}

    if "include_hidden" in st.session_state:
        params["include_hidden"] = "1" if st.session_state["include_hidden"] else "0"
    elif "include_hidden" in st.query_params:
        params["include_hidden"] = st.query_params["include_hidden"]

    for name in ("report", "bucket", "status"):
        if name in st.session_state:
            values = st.session_state[name]
            params[name] = [str(value) for value in values] or [EMPTY_FILTER_VALUE]
        else:
            values = query_values(name)
            if values:
                params[name] = values

    if "min_score" in st.session_state:
        params["min_score"] = str(st.session_state["min_score"])
    elif "min_score" in st.query_params:
        params["min_score"] = st.query_params["min_score"]

    if "search" in st.session_state:
        search_query = st.session_state["search"]
        if search_query:
            params["search"] = search_query
    elif "search" in st.query_params:
        params["search"] = st.query_params["search"]

    return params


def navigate_to(view):
    """Navigate between dashboard views without opening a new browser tab."""
    review_params = review_navigation_params()
    st.query_params.clear()
    st.query_params["view"] = view
    for name, values in review_params.items():
        st.query_params[name] = values
    st.rerun()


def render_sidebar_navigation(current_view):
    if current_view == "review":
        if st.sidebar.button("Application report →", use_container_width=True):
            navigate_to("report")
    else:
        if st.sidebar.button("← Job review", use_container_width=True):
            navigate_to("review")
    st.sidebar.divider()


def update_tracking(
    job_id,
    status,
    application_method=None,
    application_notes=None,
    skip_reason=None,
):
    """Save personal workflow data and timestamp a job the first time it is applied."""
    execute_commit(
        """
        UPDATE jobs
        SET
            application_status = %s,
            application_method = NULLIF(%s, ''),
            application_notes = NULLIF(%s, ''),
            skip_reason = NULLIF(%s, ''),
            applied_at = CASE
                WHEN %s = 'applied' THEN COALESCE(applied_at, NOW())
                ELSE applied_at
            END,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            status,
            application_method or "",
            application_notes or "",
            skip_reason or "",
            status,
            job_id,
        ),
    )


def render_tracking_form(item, key_prefix="report"):
    """Render editable workflow controls for the selected report job."""
    current_method = item["application_method"] or ""
    method_index = (
        APPLICATION_METHODS.index(current_method)
        if current_method in APPLICATION_METHODS
        else APPLICATION_METHODS.index("Other")
    )
    current_status = item["application_status"]
    status_index = (
        REPORT_APPLICATION_STATUSES.index(current_status)
        if current_status in REPORT_APPLICATION_STATUSES
        else 0
    )

    # This control sits outside the form so changing it reruns immediately and
    # only displays the skip-reason field while the job is being skipped.
    status = st.selectbox(
        "Status",
        REPORT_APPLICATION_STATUSES,
        index=status_index,
        format_func=status_label,
        key=f"{key_prefix}_status_{item['id']}",
    )

    method = st.selectbox(
        "Application method",
        APPLICATION_METHODS,
        index=method_index,
        format_func=lambda value: value or "Not recorded",
        key=f"{key_prefix}_method_{item['id']}",
    )
    notes = st.text_area(
        "Comments / notes",
        value=item["application_notes"] or "",
        placeholder="Context, follow-up details, or anything to remember about this company.",
        key=f"{key_prefix}_notes_{item['id']}",
    )
    skip_reason = ""
    if status == "skipped":
        skip_reason = st.text_area(
            "Why skip this job?",
            value=item["skip_reason"] or "",
            placeholder="Required when the status is Skipped.",
            key=f"{key_prefix}_skip_reason_{item['id']}",
        )
    if st.button(
        "Save tracking details",
        use_container_width=True,
        key=f"{key_prefix}_save_{item['id']}",
    ):
        if status == "skipped" and not skip_reason.strip():
            st.error("Please add a reason before marking this job as skipped.")
        else:
            update_tracking(item["id"], status, method, notes, skip_reason)
            st.rerun()


def render_job_review():
    st.sidebar.header("Filters & Settings")
    hidden_companies = run_query(
        "SELECT id, name, hide_reason FROM companies WHERE is_hidden = TRUE ORDER BY name ASC"
    )
    if hidden_companies:
        with st.sidebar.expander(f"Hidden Companies ({len(hidden_companies)})"):
            for company in hidden_companies:
                name_column, action_column = st.columns([3, 1])
                name_column.write(
                    f"**{company['name']}** ({company['hide_reason'] or 'No reason'})"
                )
                if action_column.button("Unhide", key=f"unhide_{company['id']}"):
                    execute_commit(
                        "UPDATE companies SET is_hidden = FALSE, hide_reason = NULL WHERE id = %s",
                        (company["id"],),
                    )
                    st.rerun()

    include_hidden = st.sidebar.checkbox(
        "Include Hidden Companies in List",
        value=st.query_params.get("include_hidden", "0") == "1",
        key="include_hidden",
    )

    reports = run_query("SELECT id, report_title, source_file FROM reports ORDER BY id DESC")
    report_options = {
        report["id"]: f"{report['report_title']} ({report['source_file']})"
        for report in reports
    }
    query_report_ids = [
        int(value)
        for value in initial_multiselect(
            "report", [str(report_id) for report_id in report_options], []
        )
    ]
    selected_report_ids = st.sidebar.multiselect(
        "Filter by Report",
        options=list(report_options.keys()),
        default=query_report_ids,
        format_func=lambda report_id: report_options[report_id],
        key="report",
    )

    buckets = ["must_apply", "strong_maybe", "maybe", "weak_fit", "hard_filtered"]
    selected_buckets = st.sidebar.multiselect(
        "Recommendation Buckets",
        options=buckets,
        default=initial_multiselect("bucket", buckets, ["strong_maybe", "maybe"]),
        key="bucket",
    )
    selected_statuses = st.sidebar.multiselect(
        "Application Status",
        options=APPLICATION_STATUSES,
        default=initial_multiselect(
            "status", APPLICATION_STATUSES, APPLICATION_STATUSES
        ),
        format_func=status_label,
        key="status",
    )
    min_score = st.sidebar.slider(
        "Minimum Match Score",
        0.0,
        100.0,
        initial_score(),
        step=5.0,
        key="min_score",
    )
    search_query = st.sidebar.text_input(
        "Search (Stack, Title, Summary)",
        st.query_params.get("search", ""),
        key="search",
    )

    persist_filters(
        include_hidden,
        selected_report_ids,
        selected_buckets,
        selected_statuses,
        min_score,
        search_query,
    )

    sql = """
        SELECT
            j.id, j.role_title, j.score, j.bucket, j.application_status,
            j.location_remote, j.salary, j.tech_stack, j.raw_apply_info,
            j.hn_comment_url, j.summary, j.why_it_fits,
            j.concerns, j.evidence_quotes, j.hard_filter_reasons, j.repost_count,
            j.application_notes, j.application_method, j.skip_reason, j.applied_at,
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
        params.extend([query_param] * 4)

    sql += " ORDER BY j.score DESC NULLS LAST"
    results = run_query(sql, params)

    st.subheader("Job review")
    st.caption("Review opportunities and track your personal application workflow.")
    metric_total, metric_priority, metric_hidden, metric_reposts = st.columns(4)
    metric_total.metric("Matches in View", len(results))
    metric_priority.metric(
        "High Priority",
        len(
            [
                result
                for result in results
                if result["bucket"] in ["must_apply", "strong_maybe"]
            ]
        ),
    )
    metric_hidden.metric("Hidden Companies", len(hidden_companies))
    metric_reposts.metric(
        "Deduplicated Reposts",
        len([result for result in results if result["repost_count"] > 1]),
    )
    st.divider()

    if not results:
        st.info("No job opportunities found matching the selected filters.")
        return

    for item in results:
        with st.container():
            repost_badge = (
                f" · Reposted {item['repost_count']}x"
                if item["repost_count"] > 1
                else ""
            )
            hidden_badge = " · Hidden" if item["is_hidden"] else ""
            score_text = f"Score: **{item['score']}** · " if item["score"] is not None else ""

            st.markdown(f"### {item['company_name']} — {item['role_title']}")
            if item["hn_comment_url"]:
                hn_url = html.escape(item["hn_comment_url"], quote=True)
                st.markdown(
                    f'<a href="{hn_url}" target="_blank" rel="noopener noreferrer">HN Thread ↗</a>',
                    unsafe_allow_html=True,
                )
            st.caption(
                f"{score_text}Bucket: {item['bucket']} · Status: "
                f"{status_label(item['application_status'])}{hidden_badge}{repost_badge}"
            )
            st.markdown(f"**Location:** {item['location_remote'] or 'Unspecified'}")
            if item["salary"]:
                st.markdown(f"**Compensation:** {item['salary']}")
            if item["tech_stack"]:
                st.markdown(f"**Tech Stack:** {item['tech_stack']}")

            email, application_url = application_contacts(item["raw_apply_info"])
            if email:
                st.markdown("**Apply via email:**")
                render_copyable_email(email)
            if application_url:
                st.link_button("Open application ↗", application_url)
            if not email and not application_url:
                st.markdown("**Apply:**")
                st.code(item["raw_apply_info"] or "Not provided in the report", language=None)

            action_columns = st.columns([1, 1.25, 1, 1.15])
            if action_columns[0].button(
                "Save for Later",
                key=f"save_{item['id']}",
                type="primary" if item["application_status"] == "saved" else "secondary",
                use_container_width=True,
            ):
                update_tracking(
                    item["id"],
                    "saved",
                    item["application_method"],
                    item["application_notes"],
                    item["skip_reason"],
                )
                st.rerun()

            with action_columns[1].popover("Mark Applied", use_container_width=True):
                method_options = APPLICATION_METHODS[1:]
                default_method = item["application_method"] or inferred_application_method(
                    item["raw_apply_info"]
                )
                method_index = (
                    method_options.index(default_method)
                    if default_method in method_options
                    else 0
                )
                method = st.selectbox(
                    "How did you apply?",
                    method_options,
                    index=method_index,
                    key=f"applied_method_{item['id']}",
                )
                notes = st.text_area(
                    "Comments / notes",
                    value=item["application_notes"] or "",
                    key=f"applied_notes_{item['id']}",
                )
                if st.button("Confirm applied", key=f"confirm_applied_{item['id']}"):
                    update_tracking(item["id"], "applied", method, notes, item["skip_reason"])
                    st.rerun()

            with action_columns[2].popover("Skip", use_container_width=True):
                skip_reason = st.text_area(
                    "Why are you skipping this job?",
                    value=item["skip_reason"] or "",
                    key=f"skip_reason_{item['id']}",
                )
                notes = st.text_area(
                    "Comments / notes (optional)",
                    value=item["application_notes"] or "",
                    key=f"skip_notes_{item['id']}",
                )
                if st.button("Confirm skip", key=f"confirm_skip_{item['id']}"):
                    if not skip_reason.strip():
                        st.error("A skip reason is required.")
                    else:
                        update_tracking(
                            item["id"],
                            "skipped",
                            item["application_method"],
                            notes,
                            skip_reason,
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


def render_application_report():
    st.subheader("Application report")
    st.caption("Select a tracked job to view its context and update its application details.")

    report_rows = run_query(
        """
        SELECT
            j.id, j.role_title, j.application_status, j.applied_at,
            j.application_method, j.application_notes, j.skip_reason,
            j.location_remote, j.raw_apply_info,
            c.name AS company_name,
            r.report_title AS found_in_report,
            r.report_date AS found_report_date
        FROM jobs j
        JOIN companies c ON j.company_id = c.id
        LEFT JOIN reports r ON j.first_seen_report_id = r.id
        WHERE j.application_status IN ('saved', 'applied', 'skipped')
        ORDER BY
            CASE j.application_status
                WHEN 'applied' THEN 1
                WHEN 'skipped' THEN 2
                ELSE 3
            END,
            j.applied_at DESC NULLS LAST,
            c.name ASC,
            j.role_title ASC
        """
    )

    applied_count = sum(item["application_status"] == "applied" for item in report_rows)
    skipped_count = sum(item["application_status"] == "skipped" for item in report_rows)
    saved_count = sum(item["application_status"] == "saved" for item in report_rows)
    st.sidebar.subheader("Tracking")
    st.sidebar.markdown(
        f"**{applied_count}** Applied · **{skipped_count}** Skipped · "
        f"**{saved_count}** Saved"
    )
    st.sidebar.divider()
    report_status_options = ["Applied", "Skipped", "Saved"]
    if "application_report_status_filter" not in st.session_state:
        st.session_state["application_report_status_filter"] = st.session_state.get(
            "_saved_application_report_statuses", report_status_options
        )
    if "application_report_filter" not in st.session_state:
        st.session_state["application_report_filter"] = st.session_state.get(
            "_saved_application_report_filter", ""
        )

    selected_statuses = st.sidebar.multiselect(
        "Current Status",
        options=report_status_options,
        key="application_report_status_filter",
    )
    filter_query = st.sidebar.text_input(
        "Filter jobs...",
        placeholder="Search by company or role title",
        key="application_report_filter",
    ).strip()
    st.session_state["_saved_application_report_statuses"] = list(selected_statuses)
    st.session_state["_saved_application_report_filter"] = filter_query

    if not report_rows:
        st.info("No tracked jobs yet. Save, apply to, or skip a job from Job review.")
        return

    table_rows = []
    table_job_ids = []
    for item in report_rows:
        table_rows.append(
            {
                "Company": item["company_name"],
                "Role Title": item["role_title"],
                "Current Status": status_label(item["application_status"]),
                "Applied On": (
                    item["applied_at"].strftime("%d %b %Y")
                    if item["applied_at"]
                    else "Not recorded"
                ),
                "Location": item["location_remote"] or "Unspecified",
            }
        )
        # Keep this parallel to the dataframe so the selected row always resolves
        # to the database primary key without exposing that key in the UI.
        table_job_ids.append(item["id"])

    table_column, detail_column = st.columns([6, 4], gap="large")
    with table_column:
        normalized_filter = filter_query.casefold()
        filtered_rows_and_ids = [
            (row, job_id)
            for row, job_id in zip(table_rows, table_job_ids)
            if row["Current Status"] in selected_statuses
            and (
                not normalized_filter
                or normalized_filter in row["Company"].casefold()
                or normalized_filter in row["Role Title"].casefold()
            )
        ]
        filtered_table_rows = [row for row, _ in filtered_rows_and_ids]
        filtered_job_ids = [job_id for _, job_id in filtered_rows_and_ids]
        table_data = pd.DataFrame(filtered_table_rows, columns=table_rows[0].keys())

        def status_cell_style(status):
            return {
                "Applied": "background-color: #e8f5e9; color: #1b5e20;",
                "Skipped": "background-color: #f3f4f6; color: #6b7280;",
                "Saved": "background-color: #eff6ff; color: #1d4ed8;",
            }.get(status, "")

        # Change the dataframe key when the filter changes to clear its selection.
        filter_revision_key = "_application_report_filter_revision"
        previous_filter_key = "_application_report_previous_filter"
        filter_signature = (normalized_filter, tuple(selected_statuses))
        if st.session_state.get(previous_filter_key) != filter_signature:
            st.session_state[previous_filter_key] = filter_signature
            st.session_state[filter_revision_key] = (
                st.session_state.get(filter_revision_key, 0) + 1
            )

        selection = st.dataframe(
            table_data.style.map(status_cell_style, subset=["Current Status"]),
            column_config={
                "Company": st.column_config.TextColumn("Company", width="small"),
                "Role Title": st.column_config.TextColumn("Role Title", width="large"),
                "Current Status": st.column_config.TextColumn(
                    "Current Status", width="small"
                ),
                "Applied On": st.column_config.TextColumn("Applied On", width="small"),
                "Location": st.column_config.TextColumn("Location", width="small"),
            },
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key=f"application_report_table_{st.session_state[filter_revision_key]}",
        )

    with detail_column:
        selected_rows = selection.selection.rows
        if not selected_rows or selected_rows[0] >= len(filtered_job_ids):
            st.markdown(
                "<div style='padding: 4rem 0; text-align: center; color: #6b7280;'>"
                "Select a job from the table to view or edit details"
                "</div>",
                unsafe_allow_html=True,
            )
            return

        selected_job_id = filtered_job_ids[selected_rows[0]]
        jobs_by_id = {item["id"]: item for item in report_rows}
        selected_item = jobs_by_id[selected_job_id]
        found_in = (
            selected_item["found_report_date"].strftime("%B %Y")
            if selected_item["found_report_date"]
            else selected_item["found_in_report"] or "Unknown report"
        )

        st.subheader("Tracking details")
        st.markdown(
            f"**Company:** {selected_item['company_name']} · "
            f"**Role:** {selected_item['role_title']} · "
            f"**Location:** {selected_item['location_remote'] or 'Unspecified'} · "
            f"**Found in:** {found_in}"
        )
        st.markdown("**Original application details:**")
        st.code(
            selected_item["raw_apply_info"] or "Not provided in the report",
            language=None,
        )

        render_tracking_form(selected_item)


current_view = st.query_params.get("view", "review")
current_view = current_view if current_view in {"review", "report"} else "review"

render_top_header()
render_sidebar_navigation(current_view)

if current_view == "review":
    render_job_review()
else:
    render_application_report()
