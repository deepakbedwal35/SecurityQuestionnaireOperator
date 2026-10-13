"""
Page 6: Inbox
View incoming vendor emails, review questionnaire attachments, and initiate automated operator runs.
Includes a preview of sent reply emails.
"""
import glob
import os
import streamlit as st
from agent.mail.mailbox import FolderMailbox
from app.service_access import get_operator_service

st.set_page_config(page_title="Customer Inbox", page_icon="📬", layout="wide")

st.title("📬 Customer Inbox")
st.caption("Incoming vendor security review emails from customers and partners.")

mailbox = FolderMailbox()
emails = mailbox.list_emails()
svc = get_operator_service(mode=st.session_state.get("llm_mode", "fake"))

tab_inbox, tab_sent = st.tabs(["📥 Inbox Messages", "📤 Sent Replies"])

with tab_inbox:
    if not emails:
        st.info("No incoming emails in inbox (`data/mailbox/inbox`).")
    else:
        st.write(f"Found **{len(emails)}** incoming message(s).")
        for msg in emails:
            with st.container(border=True):
                c_top1, c_top2 = st.columns([0.75, 0.25])
                with c_top1:
                    st.markdown(f"**From:** `{msg.from_address}` | **Date:** {msg.date}")
                    st.markdown(f"### {msg.subject}")
                with c_top2:
                    st.write("")
                    btn_handle = st.button("🚀 Handle This Email", key=f"handle_{msg.message_id}", type="primary", use_container_width=True)

                if btn_handle:
                    prompt = f"Handle email {msg.message_id} from {msg.from_address} with questionnaire"
                    new_state = svc.start_run(prompt)
                    st.session_state.current_run_id = new_state.run_id
                    st.success(f"Started operator run `{new_state.run_id}`! Redirecting to Run monitor...")
                    st.switch_page("pages/1_Run.py")

                # Attachments badge/list
                if msg.attachments:
                    st.markdown("**Attachments:**")
                    cols_att = st.columns(len(msg.attachments))
                    for idx, a in enumerate(msg.attachments):
                        with cols_att[idx]:
                            badge_color = "#10b981" if a.filename.endswith(".xlsx") else "#ef4444"
                            st.markdown(
                                f"<div style='border: 1px solid #ddd; padding: 6px 10px; border-radius: 6px; font-size: 0.85rem;'>"
                                f"📎 <b>{a.filename}</b> ({round(a.size/1024, 1)} KB)"
                                f"</div>",
                                unsafe_allow_html=True,
                            )

                with st.expander("👁️ View Message Body"):
                    st.text(msg.body_text)

with tab_sent:
    sent_files = sorted(glob.glob("data/mailbox/sent/*.eml"))
    if not sent_files:
        st.info("No sent reply emails recorded yet in `data/mailbox/sent`.")
    else:
        st.write(f"**{len(sent_files)}** sent reply email(s).")
        for sf in sent_files:
            try:
                with open(sf, "rb") as f:
                    sent_msg = FolderMailbox.parse_eml_bytes(f.read(), file_path=sf)
                with st.container(border=True):
                    st.markdown(f"**To:** `{sent_msg.to_address}` | **Date:** {sent_msg.date}")
                    st.markdown(f"**Subject:** {sent_msg.subject}")
                    if sent_msg.in_reply_to:
                        st.caption(f"In-Reply-To: `{sent_msg.in_reply_to}`")
                    if sent_msg.attachments:
                        for a in sent_msg.attachments:
                            st.markdown(f"📎 Attached: `{a.filename}` ({round(a.size/1024, 1)} KB, SHA-256: `{a.sha256[:12]}...`)")
                    with st.expander("📄 View Sent Email Body"):
                        st.text(sent_msg.body_text)
            except Exception as e:
                st.caption(f"Error reading sent message {os.path.basename(sf)}: {e}")

