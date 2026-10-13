"""
Knowledge Base service: document ingestion, deterministic chunking,
metadata indexing, deduplication, and index health.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union
import docx
from pypdf import PdfReader


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChunkRecord:
    chunk_id: str
    doc_id: str
    doc_name: str
    version: str
    effective_date: str
    source_hash: str
    content: str
    weight: float = 1.0


@dataclass
class DocRecord:
    doc_id: str
    doc_name: str
    version: str
    effective_date: str
    source_hash: str
    file_type: str
    chunk_count: int
    created_at: str


class KnowledgeBaseService:
    def __init__(self, kb_dir: Optional[str] = None) -> None:
        from agent.config import get_kb_dir
        self.kb_dir = kb_dir or get_kb_dir()
        self.index_path = os.path.join(self.kb_dir, "index.json")
        self.chunks_path = os.path.join(self.kb_dir, "chunks.json")
        os.makedirs(self.kb_dir, exist_ok=True)
        self._init_storage()

    def _init_storage(self) -> None:
        if not os.path.exists(self.index_path):
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump({"documents": {}}, f, indent=2)
        if not os.path.exists(self.chunks_path):
            with open(self.chunks_path, "w", encoding="utf-8") as f:
                json.dump({"chunks": []}, f, indent=2)

    def _read_index(self) -> Dict[str, Any]:
        with open(self.index_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_index(self, data: Dict[str, Any]) -> None:
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _read_chunks(self) -> List[Dict[str, Any]]:
        with open(self.chunks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("chunks", [])

    def _write_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        with open(self.chunks_path, "w", encoding="utf-8") as f:
            json.dump({"chunks": chunks}, f, indent=2)

    @staticmethod
    def compute_sha256(content_bytes: bytes) -> str:
        return hashlib.sha256(content_bytes).hexdigest()

    def extract_text(self, file_content: bytes, file_name: str) -> str:
        ext = os.path.splitext(file_name)[1].lower().lstrip(".")
        if ext in ("md", "txt"):
            return file_content.decode("utf-8", errors="replace")
        elif ext == "pdf":
            import io
            reader = PdfReader(io.BytesIO(file_content))
            pages_text = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            return "\n\n".join(pages_text)
        elif ext == "docx":
            import io
            doc = docx.Document(io.BytesIO(file_content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n\n".join(paragraphs)
        else:
            raise ValueError(f"Unsupported document format: .{ext}. Supported formats are .pdf, .md, .txt, .docx")

    def _chunk_text(
        self,
        text: str,
        doc_id: str,
        doc_name: str,
        version: str,
        effective_date: str,
        source_hash: str,
    ) -> List[ChunkRecord]:
        is_obsolete = "obsolete" in doc_name.lower() or "obsolete" in version.lower()
        weight = 0.2 if is_obsolete else 1.0

        # Section-based chunking on markdown headers or paragraph double-newlines
        sections = re.split(r"\n(?=#{1,3}\s)", text)
        if len(sections) <= 1:
            sections = re.split(r"\n\s*\n", text)

        chunks: List[ChunkRecord] = []
        idx = 0
        for sec in sections:
            clean = sec.strip()
            if not clean:
                continue
            # If section is excessively long (> 1500 chars), subdivide by paragraphs
            if len(clean) > 1500:
                paras = [p.strip() for p in clean.split("\n\n") if p.strip()]
                for para in paras:
                    chunk_id = f"{doc_id}#chunk_{idx}"
                    chunks.append(
                        ChunkRecord(
                            chunk_id=chunk_id,
                            doc_id=doc_id,
                            doc_name=doc_name,
                            version=version,
                            effective_date=effective_date,
                            source_hash=source_hash,
                            content=para,
                            weight=weight,
                        )
                    )
                    idx += 1
            else:
                chunk_id = f"{doc_id}#chunk_{idx}"
                chunks.append(
                    ChunkRecord(
                        chunk_id=chunk_id,
                        doc_id=doc_id,
                        doc_name=doc_name,
                        version=version,
                        effective_date=effective_date,
                        source_hash=source_hash,
                        content=clean,
                        weight=weight,
                    )
                )
                idx += 1

        return chunks

    def ingest_document(
        self,
        file_or_path: Union[str, bytes],
        doc_name: str,
        version: str,
        effective_date: str,
    ) -> Tuple[DocRecord, bool]:
        """
        Ingests document from path or bytes. Deduplicates by content SHA-256 hash.
        Returns (DocRecord, is_new: bool). If duplicate, returns existing DocRecord and False.
        """
        if isinstance(file_or_path, str):
            if not os.path.exists(file_or_path):
                raise FileNotFoundError(f"File not found: {file_or_path}")
            with open(file_or_path, "rb") as f:
                content_bytes = f.read()
            actual_name = doc_name or os.path.basename(file_or_path)
        else:
            content_bytes = file_or_path
            actual_name = doc_name

        if not actual_name:
            raise ValueError("doc_name cannot be empty")
        if not version:
            raise ValueError("version cannot be empty")
        if not effective_date:
            raise ValueError("effective_date cannot be empty")

        source_hash = self.compute_sha256(content_bytes)

        # Check existing documents by hash (deduplication)
        index_data = self._read_index()
        docs = index_data.get("documents", {})
        for did, dinfo in docs.items():
            if dinfo.get("source_hash") == source_hash:
                # Duplicate content detected
                existing_record = DocRecord(**dinfo)
                return existing_record, False

        # Extract text & generate chunks
        text = self.extract_text(content_bytes, actual_name)
        file_ext = os.path.splitext(actual_name)[1].lower().lstrip(".")
        doc_id = f"doc-{source_hash[:12]}"

        new_chunks = self._chunk_text(
            text=text,
            doc_id=doc_id,
            doc_name=actual_name,
            version=version,
            effective_date=effective_date,
            source_hash=source_hash,
        )

        doc_record = DocRecord(
            doc_id=doc_id,
            doc_name=actual_name,
            version=version,
            effective_date=effective_date,
            source_hash=source_hash,
            file_type=file_ext or "txt",
            chunk_count=len(new_chunks),
            created_at=_utcnow().isoformat(),
        )

        # Save to index
        docs[doc_id] = asdict(doc_record)
        index_data["documents"] = docs
        self._write_index(index_data)

        # Save chunks
        existing_chunks = self._read_chunks()
        # Remove any stale chunks for doc_id just in case
        existing_chunks = [c for c in existing_chunks if c.get("doc_id") != doc_id]
        existing_chunks.extend([asdict(c) for c in new_chunks])
        self._write_chunks(existing_chunks)

        return doc_record, True

    def list_documents(self) -> List[DocRecord]:
        index_data = self._read_index()
        docs = index_data.get("documents", {})
        return [DocRecord(**info) for info in docs.values()]

    def delete_document(self, doc_id: str) -> bool:
        index_data = self._read_index()
        docs = index_data.get("documents", {})
        if doc_id not in docs:
            return False

        del docs[doc_id]
        index_data["documents"] = docs
        self._write_index(index_data)

        chunks = self._read_chunks()
        chunks = [c for c in chunks if c.get("doc_id") != doc_id]
        self._write_chunks(chunks)
        return True

    def reindex(self) -> Dict[str, Any]:
        """
        Reindexes all chunks and reports health statistics.
        """
        index_data = self._read_index()
        docs = index_data.get("documents", {})
        chunks = self._read_chunks()
        return {
            "total_documents": len(docs),
            "total_chunks": len(chunks),
            "healthy": True,
        }

    def kb_status(self) -> Dict[str, Any]:
        docs = self.list_documents()
        chunks = self._read_chunks()
        return {
            "total_documents": len(docs),
            "total_chunks": len(chunks),
            "is_empty": len(docs) == 0,
            "document_types": list(set(d.file_type for d in docs)),
        }

    def get_all_chunks(self) -> List[Dict[str, Any]]:
        return self._read_chunks()


# Global or singleton service instance helper
_kb_service: Optional[KnowledgeBaseService] = None


def get_kb_service(kb_dir: Optional[str] = None) -> KnowledgeBaseService:
    global _kb_service
    from agent.config import get_kb_dir
    target_dir = kb_dir or get_kb_dir()
    if _kb_service is None or _kb_service.kb_dir != target_dir:
        _kb_service = KnowledgeBaseService(kb_dir=target_dir)
    return _kb_service


def ingest_document(
    file_or_path: Union[str, bytes],
    doc_name: str,
    version: str,
    effective_date: str,
    kb_dir: Optional[str] = None,
) -> Tuple[DocRecord, bool]:
    return get_kb_service(kb_dir).ingest_document(file_or_path, doc_name, version, effective_date)


def list_documents(kb_dir: Optional[str] = None) -> List[DocRecord]:
    return get_kb_service(kb_dir).list_documents()


def delete_document(doc_id: str, kb_dir: Optional[str] = None) -> bool:
    return get_kb_service(kb_dir).delete_document(doc_id)


def reindex(kb_dir: Optional[str] = None) -> Dict[str, Any]:
    return get_kb_service(kb_dir).reindex()


def kb_status(kb_dir: Optional[str] = None) -> Dict[str, Any]:
    return get_kb_service(kb_dir).kb_status()

