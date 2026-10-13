"""
Page 4: Run History
View all historical runs, inspect their statuses, and load them into the main viewer.
"""
import streamlit as st
from app.service_access import get_operator_service
from app.viewmodels import format_run_status

svc = get_operator_service(mode=st.session_state.get("llm_mode", "fake"))

st.title("📚 Run History")
st.caption("Inspect previous questionnaire runs recorded in SQLite checkpointer and event log.")

runs = svc.list_runs()

if not runs:
    st.info("No runs found in the current checkpoint database.")
    st.stop()

st.write(f"Total runs recorded: **{len(runs)}**")

with st.container(border=True):
    for r in runs:
        rid = r["run_id"]
        status_str = r.get("status", "unknown")
        req_text = r.get("request_text", "")
        created_at = r.get("created_at", "")
        if "T" in created_at:
            created_at = created_at.replace("T", " ")[:19]

        badge = format_run_status(status_str)

        col_id, col_req, col_badge, col_btn = st.columns([0.25, 0.45, 0.18, 0.12])

        with col_id:
            st.markdown(f"**`{rid}`**  \n<small style='color: gray;'>{created_at}</small>", unsafe_allow_html=True)
        with col_req:
            st.markdown(f"*{req_text[:80]}...*" if len(req_text) > 80 else f"*{req_text}*")
        with col_badge:
            st.markdown(f"<span style='color: {badge.color}; font-weight: bold;'>{badge.icon} {badge.label}</span>", unsafe_allow_html=True)
        with col_btn:
            if st.button("Load", key=f"load_{rid}", use_container_width=True):
                st.session_state.current_run_id = rid
                st.switch_page("pages/1_Run.py")
        st.write("---")

