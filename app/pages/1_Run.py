"""
Page 1: Run
Start new questionnaire run and monitor execution in real time with polling fragment.
"""
import streamlit as st
from app.components import (
    render_evidence_gallery,
    render_goal_card,
    render_plan_checklist,
    render_status_badge,
    render_timeline,
)
from app.service_access import get_operator_service
from app.viewmodels import (
    build_event_timeline,
    build_goal_card,
    build_plan_checklist,
    format_run_status,
)

svc = get_operator_service(mode=st.session_state.get("llm_mode", "fake"))

st.title("🚀 Questionnaire Execution")

# 1. Start Run Form
with st.container(border=True):
    col_input, col_btn = st.columns([0.82, 0.18])
    with col_input:
        req_text = st.text_input(
            "Natural Language Request",
            placeholder="Acme sent us a security questionnaire, get it done.",
            help="Free-text command describing the customer request or questionnaire task.",
            label_visibility="collapsed",
        )
    with col_btn:
        start_clicked = st.button("▶️ Start Run", type="primary", use_container_width=True)

    if start_clicked:
        if not req_text.strip():
            st.error("Please enter a request prompt.")
        else:
            with st.spinner("Starting operator run in background..."):
                new_state = svc.start_run(req_text.strip())
                st.session_state.current_run_id = new_state.run_id
                st.rerun()

current_run_id = st.session_state.get("current_run_id")

if not current_run_id:
    st.info("👈 Enter a request above or select a previous run from the sidebar to view progress.")
    st.stop()


# 2. Live Polling Fragment
@st.fragment(run_every=2)
def render_live_run(run_id: str):
    state = svc.get_run(run_id)
    if not state:
        st.warning(f"Run `{run_id}` not found or initializing...")
        return

    status_vm = format_run_status(state.status.value)

    # Top header
    col_h1, col_h2 = st.columns([0.7, 0.3])
    with col_h1:
        st.subheader(f"Run: `{run_id}`", anchor=False)
        st.caption(f"Request: *\"{state.request_text}\"*")
    with col_h2:
        st.write("")
        render_status_badge(status_vm)

    # Prominent Approval Banner
    if state.status.value == "waiting_approval":
        with st.container(border=True):
            cols_b = st.columns([0.75, 0.25])
            with cols_b[0]:
                st.warning("⚠️ **Human Approval Required!** Execution is currently paused.")
                if state.pending_approval:
                    st.markdown(f"*{state.pending_approval.reason}*")
            with cols_b[1]:
                st.write("")
                if st.button("👉 Review Approvals", type="primary", use_container_width=True):
                    st.switch_page("pages/2_Approvals.py")

    # Failure banner
    if state.status.value == "failed" and state.failure_reason:
        st.error(f"🛑 **Run Failed:** {state.failure_reason}")

    # Understood Goal
    goal_vm = build_goal_card(state.understood_goal)
    if goal_vm:
        render_goal_card(goal_vm)

    # Plan Checklist & Timeline
    col_left, col_right = st.columns([0.45, 0.55])
    with col_left:
        plan_vms = build_plan_checklist(state.plan)
        render_plan_checklist(plan_vms)

    with col_right:
        events = svc.get_events(run_id)
        timeline_rows = build_event_timeline(events)
        render_timeline(timeline_rows)

    # Evidence Gallery
    render_evidence_gallery(timeline_rows, state.artifacts)


render_live_run(current_run_id)

