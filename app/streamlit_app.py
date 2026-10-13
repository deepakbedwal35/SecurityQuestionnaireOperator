"""
Streamlit entry point: sidebar setup, run selector, and page router.
Run with: streamlit run app/streamlit_app.py
"""
import streamlit as st
from app.components import render_status_badge
from app.service_access import get_operator_service
from app.viewmodels import format_run_status

st.set_page_config(
    page_title="Security Questionnaire Operator",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 1. Initialize session state (only minimal IDs and UI flags)
if "current_run_id" not in st.session_state:
    st.session_state.current_run_id = None
if "llm_mode" not in st.session_state:
    st.session_state.llm_mode = "fake"
if "confirm_cancel" not in st.session_state:
    st.session_state.confirm_cancel = False

# 2. Sidebar controls
st.sidebar.title("🛡️ SecQ Operator")
st.sidebar.caption("Autonomous Security Questionnaire Agent")

# LLM Mode Selector
mode = st.sidebar.radio(
    "LLM Mode",
    options=["fake", "real"],
    index=0 if st.session_state.llm_mode == "fake" else 1,
    horizontal=True,
    help="'fake' uses deterministic pre-canned responses for instant offline testing. 'real' connects to Groq API.",
)
st.session_state.llm_mode = mode
svc = get_operator_service(mode=st.session_state.llm_mode)

# Run Selector
runs = svc.list_runs()
run_options = ["None"] + [r["run_id"] for r in runs]
current_idx = 0
if st.session_state.current_run_id in run_options:
    current_idx = run_options.index(st.session_state.current_run_id)

selected_run = st.sidebar.selectbox(
    "Active Run",
    options=run_options,
    index=current_idx,
)
if selected_run != "None":
    st.session_state.current_run_id = selected_run
elif selected_run == "None" and st.session_state.current_run_id is not None:
    # If user explicitly selected None
    if current_idx != 0:
        st.session_state.current_run_id = None

# Active Run Status & Cancel
if st.session_state.current_run_id:
    run_state = svc.get_run(st.session_state.current_run_id)
    if run_state:
        st.sidebar.markdown("---")
        st.sidebar.markdown(f"**Run:** `{run_state.run_id}`")
        status_vm = format_run_status(run_state.status.value)
        render_status_badge(status_vm)

        # Cancel button (with confirmation)
        if not status_vm.is_terminal:
            st.sidebar.markdown("")
            if not st.session_state.confirm_cancel:
                if st.sidebar.button("⏹️ Cancel Run", use_container_width=True):
                    st.session_state.confirm_cancel = True
                    st.rerun()
            else:
                st.sidebar.warning("Confirm cancel this run?")
                col_c1, col_c2 = st.sidebar.columns(2)
                with col_c1:
                    if st.button("Yes", key="btn_cancel_yes", use_container_width=True):
                        svc.cancel_run(st.session_state.current_run_id)
                        st.session_state.confirm_cancel = False
                        st.rerun()
                with col_c2:
                    if st.button("No", key="btn_cancel_no", use_container_width=True):
                        st.session_state.confirm_cancel = False
                        st.rerun()

# Pending Approvals Badge on Navigation
pending_count = 0
if st.session_state.current_run_id:
    appr = svc.get_pending_approval(st.session_state.current_run_id)
    if appr:
        pending_count = len(appr.items) if appr.items else 1

if pending_count > 0:
    st.sidebar.markdown("---")
    st.sidebar.warning(f"⚠️ **1 pending approval ({pending_count} items)** awaiting review in **Approvals** tab.")

st.sidebar.markdown("---")
st.sidebar.caption("SecQ Operator v0.3.0 | Founding Engineer Demo")

# Welcome / routing prompt if opened root
st.markdown("## Welcome to Security Questionnaire Operator")
st.markdown(
    """
Select a page from the sidebar navigation:
- **1_Run**: Start and monitor a questionnaire fulfillment run with live event timeline and plan checklist.
- **2_Approvals**: Review batched human-in-the-loop decisions (grounded answers, commitments, policy overrides).
- **3_Report**: Inspect audit evidence, verified postconditions, and download populated spreadsheets.
- **4_History**: Browse historical operator runs.
"""
)
if st.session_state.current_run_id:
    st.info(f"👉 Current active run: `{st.session_state.current_run_id}`. Navigate to **1_Run** to view live progress.")
else:
    st.info("💡 Go to **1_Run** to initiate your first security questionnaire run.")

