"""
Page 5: Knowledge Base
Ingest, view, search, and manage authoritative company policy documents.
Supports deterministic chunking, de-duplication, and multi-format extraction (.pdf, .md, .txt, .docx).
"""
import streamlit as st
from agent.kb import (
    delete_document,
    ingest_document,
    kb_status,
    list_documents,
    reindex,
)

st.set_page_config(page_title="Knowledge Base", page_icon="📚", layout="wide")

st.title("📚 Knowledge Base")
st.caption("Authoritative company security policies, certifications, and compliance procedures.")

status = kb_status()
docs = list_documents()

# Top metric row
col_m1, col_m2, col_m3 = st.columns(3)
col_m1.metric("Indexed Documents", status.get("total_documents", 0))
col_m2.metric("Total Chunks", status.get("total_chunks", 0))
col_m3.metric("Supported Formats", "PDF, Markdown, TXT, DOCX")

st.divider()

# 1. Document Upload Section
with st.expander("📤 Ingest New Policy Document", expanded=len(docs) == 0):
    with st.form("form_kb_upload", clear_on_submit=True):
        uploaded_files = st.file_uploader(
            "Choose policy documents to ingest",
            type=["pdf", "md", "txt", "docx"],
            accept_multiple_files=True,
            help="Supported: PDF, Markdown (.md), Plain Text (.txt), Word Document (.docx)",
        )
        col_u1, col_u2 = st.columns(2)
        with col_u1:
            doc_version = st.text_input("Document Version", value="1.0", placeholder="e.g. 2.1 or 2024.1")
        with col_u2:
            effective_date = st.text_input("Effective Date", value="2024-01-01", placeholder="YYYY-MM-DD")

        submit_upload = st.form_submit_button("📥 Ingest Documents", type="primary")

    if submit_upload:
        if not uploaded_files:
            st.error("Please select at least one file to upload.")
        elif not doc_version.strip() or not effective_date.strip():
            st.error("Document Version and Effective Date are required.")
        else:
            success_count = 0
            dup_count = 0
            for uf in uploaded_files:
                file_bytes = uf.read()
                fname = uf.name
                doc_rec, is_new = ingest_document(
                    file_or_path=file_bytes,
                    doc_name=fname,
                    version=doc_version.strip(),
                    effective_date=effective_date.strip(),
                )
                if is_new:
                    success_count += 1
                else:
                    dup_count += 1

            if success_count > 0:
                st.success(f"✅ Successfully ingested {success_count} document(s).")
            if dup_count > 0:
                st.info(f"ℹ️ {dup_count} duplicate document(s) detected (identical content hash); skipped re-indexing.")
            st.rerun()

# 2. Documents Table
st.subheader("📑 Indexed Documents", anchor=False)

if not docs:
    st.warning("⚠️ Knowledge base is currently empty. Upload policies above to enable the questionnaire operator.")
else:
    table_data = []
    for d in docs:
        table_data.append({
            "Doc ID": d.doc_id,
            "Document Name": d.doc_name,
            "Version": d.version,
            "Effective Date": d.effective_date,
            "Type": d.file_type.upper(),
            "Chunks": d.chunk_count,
            "Content Hash": d.source_hash[:12] + "...",
            "Created": d.created_at[:19],
        })
    st.dataframe(table_data, use_container_width=True, hide_index=True)

    # Management controls
    st.markdown("### Document Management")
    c_del1, c_del2 = st.columns([0.7, 0.3])
    with c_del1:
        doc_to_delete = st.selectbox(
            "Select document to delete",
            options=[d.doc_id for d in docs],
            format_func=lambda x: next((f"{d.doc_name} (v{d.version}, {x})" for d in docs if d.doc_id == x), x),
        )
    with c_del2:
        st.write("")
        if st.button("🗑️ Delete Document", type="secondary", use_container_width=True):
            if delete_document(doc_to_delete):
                st.success(f"Deleted document {doc_to_delete}.")
                st.rerun()

st.divider()

# 3. Index Health & Reindex
col_h1, col_h2 = st.columns([0.8, 0.2])
with col_h1:
    st.caption("Need to rebuild the retrieval index? Reindex parses and refreshes all chunk embeddings and metadata.")
with col_h2:
    if st.button("🔄 Reindex Knowledge Base", use_container_width=True):
        info = reindex()
        st.success(f"Reindexed {info['total_documents']} documents ({info['total_chunks']} chunks). Index healthy.")

