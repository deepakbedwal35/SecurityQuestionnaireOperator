"""
Retriever interface, plug-in adapter for external production RAG (QueryDocsAI),
local keyword/chunk retriever, and retrieve_evidence tool.
"""
from abc import ABC, abstractmethod
import glob
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.memory import FeedbackStore
from agent.policy import PolicyEngine
from agent.state import AnswerDecision, Citation, QuestionType, RiskLevel, RunState
from agent.tools.registry import Tool


class RetrievalResult(BaseModel):
    doc_id: str
    chunk_id: str
    content: str
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Retriever(ABC):
    """
    Abstract adapter interface for retrieval.
    Plug in existing production RAG (QueryDocsAI: BM25 + dense bge-base + Qdrant + cross-encoder) here.
    """
    @abstractmethod
    def retrieve(self, query: str, top_k: int = 5) -> List[RetrievalResult]:
        pass


class QueryDocsAIAdapter(Retriever):
    """
    Adapter placeholder for the user's existing production RAG project.
    Points to <path or repo of QueryDocsAI>.
    """
    def __init__(self, query_docs_ai_path: Optional[str] = None) -> None:
        self.query_docs_ai_path = query_docs_ai_path or os.getenv("QUERYDOCSAI_PATH", "../QueryDocsAI")
        self._is_available = os.path.exists(self.query_docs_ai_path)

    def retrieve(self, query: str, top_k: int = 5) -> List[RetrievalResult]:
        if not self._is_available:
            raise NotImplementedError(
                f"QueryDocsAI not found at '{self.query_docs_ai_path}'. Use KeywordPolicyRetriever for local mode."
            )
        # In production integration:
        # from query_docs_ai import search_pipeline
        # return search_pipeline.search(query, top_k=top_k)
        raise NotImplementedError("Direct QueryDocsAI integration to be connected.")


class KeywordPolicyRetriever(Retriever):
    """
    Local chunking & keyword-matching retriever across policy documents in Knowledge Base (data/kb)
    and fallback policies in data/policies/.
    Respects document versions and authoritativeness (e.g. newer effective_date supersedes older).
    """
    def __init__(
        self,
        policies_dir: Optional[str] = None,
        feedback_store: Optional[FeedbackStore] = None,
        kb_dir: Optional[str] = None,
    ) -> None:
        from agent.config import get_kb_dir, get_policies_dir
        self.policies_dir = policies_dir or get_policies_dir()
        self.kb_dir = kb_dir or get_kb_dir()
        self.feedback_store = feedback_store or FeedbackStore()
        self.chunks: List[Dict[str, Any]] = []
        self._load_and_chunk()

    def _load_and_chunk(self) -> None:
        from agent.kb import get_kb_service
        kb = get_kb_service(self.kb_dir)
        kb_chunks = kb.get_all_chunks()
        if kb_chunks:
            for c in kb_chunks:
                self.chunks.append({
                    "doc": c.get("doc_name", "policy"),
                    "chunk_id": c.get("chunk_id", ""),
                    "content": c.get("content", ""),
                    "weight": c.get("weight", 1.0),
                    "version": c.get("version", ""),
                    "effective_date": c.get("effective_date", "1970-01-01"),
                })
            return

        # Fallback to loading files from policies_dir if KB empty
        if os.path.exists(self.policies_dir):
            policy_files = glob.glob(os.path.join(self.policies_dir, "*.md"))
            for pfile in policy_files:
                filename = os.path.basename(pfile)
                is_obsolete = "obsolete" in filename.lower()
                try:
                    with open(pfile, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    continue

                sections = content.split("## ")
                for idx, sec in enumerate(sections):
                    text = sec.strip()
                    if not text:
                        continue
                    weight = 0.2 if is_obsolete else 1.0
                    self.chunks.append({
                        "doc": filename,
                        "chunk_id": f"{filename}#sec_{idx}",
                        "content": text,
                        "weight": weight,
                        "version": "1.0",
                        "effective_date": "2022-01-01" if is_obsolete else "2024-01-01",
                    })

    def retrieve(self, query: str, top_k: int = 5) -> List[RetrievalResult]:
        query_words = set(re.findall(r"\w+", query.lower()))
        stopwords = {"what", "is", "the", "are", "do", "you", "does", "your", "for", "in", "and", "to", "how", "our", "we"}
        filtered_query = [w for w in query_words if len(w) > 2 and w not in stopwords]

        scored: List[tuple[float, str, Dict[str, Any]]] = []
        for chunk in self.chunks:
            c_text = chunk["content"].lower()
            match_count = sum(1 for w in filtered_query if w in c_text)
            if match_count > 0:
                score = (match_count / max(1, len(filtered_query))) * chunk["weight"]
                eff_date = chunk.get("effective_date", "1970-01-01")
                scored.append((score, eff_date, chunk))

        # Sort primarily by score, and secondarily by effective_date (latest first)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        results: List[RetrievalResult] = []
        for score, eff_date, chunk in scored[:top_k]:
            results.append(RetrievalResult(
                doc_id=chunk["doc"],
                chunk_id=chunk["chunk_id"],
                content=chunk["content"],
                score=round(score, 3),
                metadata={
                    "weight": chunk["weight"],
                    "version": chunk.get("version"),
                    "effective_date": eff_date,
                },
            ))
        return results


# ---- Tool: retrieve_evidence ----

class RetrieveEvidenceInput(BaseModel):
    question_id: str
    question_text: str


class RetrieveEvidenceOutput(BaseModel):
    question_id: str
    decision: AnswerDecision
    answer_text: str
    confidence: float
    citations: List[Citation]
    conflict_note: Optional[str] = None


class RetrieveEvidenceTool(Tool):
    name = "retrieve_evidence"
    description = "Retrieves evidence from policies and feedback store to synthesize an answer or recommend escalation."
    risk = RiskLevel.READ
    args_schema = RetrieveEvidenceInput
    output_schema = RetrieveEvidenceOutput

    def __init__(self, retriever: Optional[Retriever] = None, feedback_store: Optional[FeedbackStore] = None) -> None:
        self.retriever = retriever or KeywordPolicyRetriever()
        self.feedback_store = feedback_store or FeedbackStore()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        qid = kwargs["question_id"]
        qtext = kwargs["question_text"]

        # Check prompt injection
        is_inj, inj_snip = PolicyEngine.detect_prompt_injection(qtext)
        if is_inj:
            return {
                "question_id": qid,
                "decision": AnswerDecision.FLAGGED,
                "answer_text": f"Blocked: Prompt injection heuristic triggered ('{inj_snip}').",
                "confidence": 1.0,
                "citations": [],
                "conflict_note": "Security violation flagged.",
            }

        # Check commitment / legal clause
        is_commit, commit_snip = PolicyEngine.is_commitment_question(qtext)
        if is_commit:
            return {
                "question_id": qid,
                "decision": AnswerDecision.ESCALATED,
                "answer_text": f"Escalated to Legal/CISO: Involves binding legal/SLA commitment ('{commit_snip}').",
                "confidence": 0.95,
                "citations": [Citation(doc="Security_Questionnaire_SOP.md", quote="Mandatory Escalation: SLA commitments and liability clauses")],
                "conflict_note": "Requires human sign-off per SOP.",
            }

        # Check human feedback store first (learning from feedback)
        fb_matches = self.feedback_store.search(qtext, top_k=1)
        if fb_matches:
            rule = fb_matches[0]
            return {
                "question_id": qid,
                "decision": AnswerDecision.ANSWERED,
                "answer_text": f"{rule.answer} (Approved by {rule.approver})",
                "confidence": 0.98,
                "citations": [Citation(doc="feedback_rules.json", chunk_id=rule.id, quote=rule.answer)],
                "conflict_note": f"Grounded via feedback store rule {rule.id}",
            }

        # Search policy docs
        retrieved = self.retriever.retrieve(qtext, top_k=3)
        if not retrieved or retrieved[0].score < 0.25:
            # Low confidence or out of scope
            return {
                "question_id": qid,
                "decision": AnswerDecision.ESCALATED,
                "answer_text": "Escalated: Not covered in current company policies or certifications.",
                "confidence": 0.2,
                "citations": [],
                "conflict_note": "Insufficient grounding in policy documentation.",
            }

        top = retrieved[0]
        # Check if obsolete doc was also matched (conflict detection)
        conflict_note = None
        if any("obsolete" in r.doc_id.lower() for r in retrieved):
            conflict_note = "Resolved conflict: v2 policy takes precedence over obsolete v1."

        # Extract answer snippet (first 2-3 lines of matching chunk)
        lines = [line.strip() for line in top.content.split("\n") if line.strip() and not line.startswith("#")]
        answer_candidate = " ".join(lines[:3]) if lines else top.content[:200]

        return {
            "question_id": qid,
            "decision": AnswerDecision.ANSWERED,
            "answer_text": answer_candidate,
            "confidence": min(1.0, top.score),
            "citations": [Citation(doc=top.doc_id, chunk_id=top.chunk_id, quote=answer_candidate[:120])],
            "conflict_note": conflict_note,
        }

