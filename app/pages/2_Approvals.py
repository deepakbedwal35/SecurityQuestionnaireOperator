"""
Page 2: Approvals
Human-in-the-loop approval management for ANSWER_REVIEW and FINAL_SIGNOFF.
Enforces legal commitment policy and citations in code.
"""
import streamlit as st
from agent.state import (
    ApprovalDecision,
    ApprovalItemDecision,
    ApprovalKind,
    ItemAction,
)
from agent.service import ValidationError
from app.service_access import get_operator_service
from app.viewmodels import (
    build_approval_item_card,
    build_final_signoff_summary,
    format_run_status,
)

svc = get_operator_service(mode=st.session_state.get("llm_mode", "fake"))

st.title("🛡️ Human Approvals & Signoff")

current_run_id = st.session_state.get("current_run_id")

if not current_run_id:
    st.info("👈 Please select or start a run first.")
    st.stop()

# Header status
state = svc.get_run(current_run_id)
if not state:
    st.error(f"Run `{current_run_id}` not found.")
    st.stop()

status_vm = format_run_status(state.status.value)
col_h1, col_h2 = st.columns([0.75, 0.25])
with col_h1:
    st.subheader(f"Run: `{current_run_id}`", anchor=False)
    st.caption(f"Status: **{status_vm.label}**")
with col_h2:
    if st.button("⬅️ Back to Run Live View", use_container_width=True):
        st.switch_page("pages/1_Run.py")

pending = svc.get_pending_approval(current_run_id)

# 1. No pending approval
if not pending:
    st.success("✅ **No pending approvals for this run.**")
    st.markdown("All questions and steps are either executing, completed, or already resolved.")

    # Show resolved approvals history from timeline
    events = svc.get_events(current_run_id)
    resolved_events = [e for e in events if e.type == "approval_resolved"]
    if resolved_events:
        st.divider()
        st.subheader("📜 Approval Resolution History", anchor=False)
        for ev in resolved_events:
            p = ev.payload or {}
            with st.container(border=True):
                st.markdown(f"**Approval ID:** `{p.get('approval_id')}` | **Decided by:** `{p.get('decided_by')}`")
                st.markdown(f"**Outcome:** `{p.get('status')}`")
                if "decisions_count" in p:
                    st.caption(f"Resolved {p.get('decisions_count')} items.")
    st.stop()


# 2. Pending Approval Found
st.warning(f"⚠️ **Pending Approval Required: {pending.kind.value}**")
st.markdown(f"**Reason:** {pending.reason}")
st.divider()

# Session state flags for double-submit guard
submitting_key = f"submitting_{pending.id}"
if submitting_key not in st.session_state:
    st.session_state[submitting_key] = False


# -------------------------------------------------------------
# Case A: ANSWER_REVIEW (Batch form)
# -------------------------------------------------------------
if pending.kind == ApprovalKind.ANSWER_REVIEW:
    st.subheader(f"📝 Review Escalated Questions ({len(pending.items)} items)", anchor=False)
    st.caption("Review each question below. Commitment questions must be edited or rejected per company policy.")

    item_cards = [build_approval_item_card(item) for item in pending.items]

    # Use a single form for batch review
    with st.form(key=f"form_approval_{pending.id}"):
        decisions_input: dict[str, dict] = {}

        for idx, card in enumerate(item_cards):
            with st.container(border=True):
                # Header of item card
                c_head1, c_head2 = st.columns([0.7, 0.3])
                with c_head1:
                    st.markdown(f"**[{card.question_id}]** {card.question_text}")
                with c_head2:
                    st.markdown(f"<span style='float:right; font-size: 0.8rem; background: #e0f2fe; color: #0369a1; padding: 2px 8px; border-radius: 6px;'>{card.type_badge}</span>", unsafe_allow_html=True)

                st.caption(f"**Escalation Reason:** {card.reason_for_escalation}")

                # Citations and draft preview
                if card.draft_answer:
                    st.markdown(f"**Draft Answer:**  \n> {card.draft_answer}")
                else:
                    st.markdown("*No draft answer generated (evidence missing or escalated before answer).*")

                if card.citations:
                    with st.expander(f"📚 Grounded Citations ({len(card.citations)})", expanded=False):
                        for cit in card.citations:
                            st.markdown(f"- **Source:** `{cit.doc}`")
                            st.markdown(f"  > *\"{cit.quote}\"*")

                # Policy restriction callout
                if not card.can_approve:
                    st.info(f"ℹ️ **Constraint:** {card.cannot_approve_reason}")

                st.write("---")

                # Action choices
                action_options = ["edit", "reject"]
                if card.can_approve:
                    action_options = ["approve", "edit", "reject"]

                chosen_action = st.radio(
                    f"Decision for {card.question_id}:",
                    options=action_options,
                    format_func=lambda x: {
                        "approve": "✅ Approve draft answer",
                        "edit": "✏️ Edit / Provide human answer",
                        "reject": "❌ Reject (Leave unanswered / decline)",
                    }[x],
                    index=0 if card.can_approve else 0,
                    key=f"action_{card.question_id}",
                    horizontal=True,
                )

                # Editable text input
                initial_text = card.draft_answer or ""
                edited_answer = st.text_area(
                    f"Human Answer for {card.question_id} (required if Edit selected):",
                    value=initial_text,
                    key=f"text_{card.question_id}",
                    height=80,
                )

                item_note = st.text_input(
                    f"Optional note / reason for {card.question_id}:",
                    key=f"note_{card.question_id}",
                )

                decisions_input[card.question_id] = {
                    "action": chosen_action,
                    "edited_answer": edited_answer,
                    "note": item_note,
                    "card": card,
                }

        # Submit bar
        submit_btn = st.form_submit_button(
            "🚀 Submit Decisions & Resume Operator",
            type="primary",
            use_container_width=True,
            disabled=st.session_state[submitting_key],
        )

    if submit_btn:
        st.session_state[submitting_key] = True
        try:
            item_decisions = []
            validation_errors = []

            for qid, d in decisions_input.items():
                act = ItemAction(d["action"])
                ans_txt = d["edited_answer"].strip()
                card = d["card"]

                if act == ItemAction.EDIT and not ans_txt:
                    validation_errors.append(f"Question '{qid}' is set to Edit but no edited answer was provided.")

                if act == ItemAction.APPROVE and not card.can_approve:
                    validation_errors.append(f"Question '{qid}' cannot be approved: {card.cannot_approve_reason}")

                item_decisions.append(ApprovalItemDecision(
                    question_id=qid,
                    action=act,
                    edited_answer=ans_txt if act == ItemAction.EDIT else None,
                    note=d["note"].strip() or None,
                ))

            if validation_errors:
                st.session_state[submitting_key] = False
                for err in validation_errors:
                    st.error(f"❌ {err}")
            else:
                decision = ApprovalDecision(
                    approval_id=pending.id,
                    decisions=item_decisions,
                    decided_by="human_operator",
                )
                with st.spinner("Submitting decisions and resuming run in background..."):
                    updated_state = svc.resolve_approval(current_run_id, pending.id, decision)
                    st.session_state[submitting_key] = False
                    st.success("✅ Decisions resolved successfully! Resuming workflow...")
                    st.switch_page("pages/1_Run.py")

        except ValidationError as ve:
            st.session_state[submitting_key] = False
            st.error(f"❌ **Validation Error:** {ve}")
        except Exception as ex:
            st.session_state[submitting_key] = False
            st.error(f"❌ **Unexpected Error:** {ex}")


# -------------------------------------------------------------
# Case B: FINAL_SIGNOFF
# -------------------------------------------------------------
elif pending.kind == ApprovalKind.FINAL_SIGNOFF:
    st.subheader("📑 Final Questionnaire Signoff", anchor=False)
    st.caption("All questions have been evaluated and filled into the questionnaire. Review the final answers before submission.")

    signoff_vm = build_final_signoff_summary(state)

    # Metrics summary
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("Total Questions", signoff_vm.total_count)
    col_m2.metric("Answered", signoff_vm.answered_count)
    col_m3.metric("Human Overrides", signoff_vm.overridden_count)
    col_m4.metric("Unresolved", signoff_vm.unresolved_count)

    # Download filled xlsx if available
    review_path = signoff_vm.review_xlsx_path
    if review_path:
        import os
        if os.path.exists(review_path):
            with open(review_path, "rb") as f:
                bytes_data = f.read()
            st.download_button(
                "📥 Download Filled Questionnaire (.xlsx)",
                data=bytes_data,
                file_name=os.path.basename(review_path),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.caption(f"Filled file planned at: `{review_path}`")

    # Email Draft Preview (for email delivery runs)
    if state.artifacts.get("email_draft_to"):
        with st.container(border=True):
            st.markdown("### ✉️ Outgoing Email Draft Preview")
            st.markdown(f"**Recipient (To):** `{state.artifacts.get('email_draft_to')}`")
            st.markdown(f"**Subject:** `{state.artifacts.get('email_draft_subject')}`")
            st.markdown(f"**Attachment:** `{state.artifacts.get('email_draft_attachment')}` (Hash: `{state.artifacts.get('email_draft_hash', '')[:16]}...`)")
            st.caption("Clean customer-facing text (internal scores, flags, and reasons stripped):")
            st.code(state.artifacts.get("email_draft_body", ""), language="text")

    # Preview table
    st.write("### Answers Preview")
    if signoff_vm.preview_answers:
        st.dataframe(signoff_vm.preview_answers, use_container_width=True, hide_index=True)

    st.write("---")
    st.write("### Signoff Decision")
    c_btn1, c_btn2 = st.columns(2)

    with c_btn1:
        if st.button("✅ Approve & Finalize", type="primary", use_container_width=True, disabled=st.session_state[submitting_key]):
            st.session_state[submitting_key] = True
            decision = ApprovalDecision(
                approval_id=pending.id,
                final_action=ItemAction.APPROVE,
                decided_by="human_operator",
            )
            with st.spinner("Submitting final signoff..."):
                svc.resolve_approval(current_run_id, pending.id, decision)
                st.session_state[submitting_key] = False
                st.success("✅ Questionnaire approved! Run completed.")
                st.switch_page("pages/1_Run.py")

    with c_btn2:
        if st.button("🛑 Reject Signoff", type="secondary", use_container_width=True, disabled=st.session_state[submitting_key]):
            st.session_state[submitting_key] = True
            decision = ApprovalDecision(
                approval_id=pending.id,
                final_action=ItemAction.REJECT,
                decided_by="human_operator",
            )
            with st.spinner("Submitting final rejection..."):
                svc.resolve_approval(current_run_id, pending.id, decision)
                st.session_state[submitting_key] = False
                st.warning("🛑 Signoff rejected.")
                st.switch_page("pages/1_Run.py")

