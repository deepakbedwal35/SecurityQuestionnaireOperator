"""
Page 3: Report & Evidence
Final evaluation results, postconditions, question-by-question citations, and artifact downloads.
"""
import os
import streamlit as st
from app.components import render_status_badge
from app.service_access import get_operator_service
from app.viewmodels import build_report_view

svc = get_operator_service(mode=st.session_state.get("llm_mode", "fake"))

st.title("📊 Run Report & Evidence")

current_run_id = st.session_state.get("current_run_id")

if not current_run_id:
    st.info("👈 Please select or start a run first.")
    st.stop()

# Fetch report and state
report_data = svc.get_report(current_run_id)
state = svc.get_run(current_run_id)

if not state and "error" in report_data:
    st.error(report_data.get("error", f"Run {current_run_id} not found."))
    st.stop()

report_vm = build_report_view(report_data, state)

# Top Bar
col_h1, col_h2 = st.columns([0.75, 0.25])
with col_h1:
    st.subheader(f"Run: `{report_vm.run_id}`", anchor=False)
with col_h2:
    render_status_badge(report_vm.status)

if report_vm.failure_reason:
    st.error(f"🛑 **Failure Reason:** {report_vm.failure_reason}")

# Metrics cards
st.write("---")
m_cols = st.columns(6)
m_cols[0].metric("Parsed", report_vm.parsed_count)
m_cols[1].metric("Processed", report_vm.processed_count)
m_cols[2].metric("Answered", report_vm.answered_count)
m_cols[3].metric("Escalated", report_vm.escalated_count)
m_cols[4].metric("Flagged", report_vm.flagged_count)
m_cols[5].metric("Unresolved", report_vm.unresolved_count)

# Artifact Downloads
st.write("### 📦 Deliverables & Artifacts")
art_cols = st.columns(3)

# 1. Final / Filled xlsx
filled_xlsx_path = (
    report_vm.artifacts.get("final_xlsx")
    or report_vm.artifacts.get("filled_xlsx")
    or "data/output/Acme_Security_Questionnaire_Filled.xlsx"
)
with art_cols[0]:
    if os.path.exists(filled_xlsx_path):
        with open(filled_xlsx_path, "rb") as f:
            st.download_button(
                "📥 Download Filled Questionnaire (.xlsx)",
                data=f.read(),
                file_name=os.path.basename(filled_xlsx_path),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
    else:
        st.button("📥 Questionnaire (.xlsx) Not Ready", disabled=True, use_container_width=True)

# 2. Markdown Report
report_md_path = report_vm.artifacts.get("report_md") or f"artifacts/{current_run_id}/report.md"
with art_cols[1]:
    if os.path.exists(report_md_path):
        with open(report_md_path, "rb") as f:
            st.download_button(
                "📄 Download Executive Report (.md)",
                data=f.read(),
                file_name=f"report_{current_run_id}.md",
                mime="text/markdown",
                use_container_width=True,
            )
    else:
        st.button("📄 Executive Report (.md) Not Ready", disabled=True, use_container_width=True)

# 3. Evidence JSON
evidence_json_path = report_vm.artifacts.get("evidence_json") or f"artifacts/{current_run_id}/evidence.json"
with art_cols[2]:
    if os.path.exists(evidence_json_path):
        with open(evidence_json_path, "rb") as f:
            st.download_button(
                "🧾 Download Full Evidence Pack (.json)",
                data=f.read(),
                file_name=f"evidence_{current_run_id}.json",
                mime="application/json",
                use_container_width=True,
            )
    else:
        st.button("🧾 Evidence Pack (.json) Not Ready", disabled=True, use_container_width=True)


# Postconditions Table
st.write("---")
st.subheader("🛡️ Machine-Checkable Verification Postconditions", anchor=False)
if report_vm.postconditions:
    st.dataframe(report_vm.postconditions, use_container_width=True, hide_index=True)
else:
    st.info("No postcondition results logged yet.")


# Detailed Answers Breakdown
st.write("---")
st.subheader("📑 Question & Evidence Breakdown", anchor=False)

if not report_vm.questions:
    st.info("No question answers processed yet.")
else:
    # Filter options
    filter_choice = st.radio(
        "Filter by Decision:",
        options=["All", "answered", "escalated", "flagged"],
        horizontal=True,
    )

    filtered_qs = [
        q for q in report_vm.questions
        if filter_choice == "All" or q.decision.lower() == filter_choice.lower()
    ]

    st.caption(f"Showing {len(filtered_qs)} of {len(report_vm.questions)} questions")

    for q in filtered_qs:
        icon = "✅" if q.decision.lower() == "answered" else ("⚠️" if q.decision.lower() == "escalated" else "🚩")
        title = f"{icon} [{q.id}] {q.text[:85]}..."

        with st.expander(title, expanded=False):
            st.markdown(f"**Full Question:** {q.text}")
            st.markdown(f"**Decision:** `{q.decision.upper()}` | **Confidence:** `{q.confidence:.2f}`")

            if q.human_override:
                st.markdown(f"**Human Override:** `{q.human_override}`")

            if q.flags:
                st.markdown(f"**Flags:** {', '.join(q.flags)}")

            st.write("---")
            st.markdown(f"**Answer / Draft:**  \n> {q.answer_text if q.answer_text else '*(No answer generated)*'}")

            if q.citations:
                st.write("**Evidence & Grounded Citations:**")
                for cit in q.citations:
                    st.markdown(f"- **Source:** `{cit.doc}`")
                    st.markdown(f"  > *\"{cit.quote}\"*")
            else:
                st.caption("No supporting citations available.")

# Portal Evidence Gallery (Screenshots)
screen_dir = os.path.join("artifacts", current_run_id, "screens")
screenshots = []
if os.path.exists(screen_dir):
    screenshots = [
        os.path.join(screen_dir, f) for f in sorted(os.listdir(screen_dir)) if f.endswith(".png")
    ]

if screenshots:
    st.write("---")
    st.subheader("📸 Portal Submission Proof & Screenshots", anchor=False)
    for s_path in screenshots:
        s_name = os.path.basename(s_path).replace(".png", "").replace("_", " ").title()
        st.caption(f"Captured: **{s_name}** (`{s_path}`)")
        st.image(s_path, use_container_width=True)

# Raw Markdown Report Tab
if report_vm.raw_markdown:
    st.write("---")
    with st.expander("📝 View Raw Generated Markdown Report", expanded=False):
        st.markdown(report_vm.raw_markdown)

