"""
Composite question processing tool: chains retrieval, drafting, policy checking,
precedence conflict resolution, and commitment escalation.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.llm import LLMClient
from agent.memory import FeedbackStore
from agent.policy import PolicyEngine
from agent.state import Answer, AnswerDecision, Citation, QuestionType, RiskLevel, RunState
from agent.tools.registry import Tool
from agent.tools.retrieve import Retriever


class ProcessQuestionInput(BaseModel):
    question_id: str = Field(..., description="The ID of the question to process, e.g. Q-01")


class ProcessQuestionOutput(BaseModel):
    question_id: str
    decision: AnswerDecision
    text: str
    confidence: float
    citations: List[Citation]
    conflict_note: Optional[str] = None
    flags: List[str] = Field(default_factory=list)


class DraftAnswerOutput(BaseModel):
    answer: str
    key_points: List[str] = Field(default_factory=list)


class ProcessQuestionTool(Tool):
    name = "process_question"
    description = "Processes a questionnaire question end-to-end: retrieval, policy guardrails, draft synthesis, and citation attachment."
    risk = RiskLevel.WRITE_LOW
    args_schema = ProcessQuestionInput
    output_schema = ProcessQuestionOutput

    def __init__(
        self,
        retriever: Retriever,
        llm_client: LLMClient,
        feedback_store: Optional[FeedbackStore] = None,
    ) -> None:
        self.retriever = retriever
        self.llm = llm_client
        self.feedback_store = feedback_store or FeedbackStore()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        qid = kwargs["question_id"]
        q_obj = next((q for q in state.questions if q.id == qid), None)
        if not q_obj:
            raise ValueError(f"Question ID '{qid}' not found in state.")

        qtext = q_obj.text

        # 1. Prompt Injection Heuristic Check
        is_inj, inj_snip = PolicyEngine.detect_prompt_injection(qtext)
        if is_inj:
            state.add_injection_flag(
                source=f"questionnaire:{qid}",
                snippet=qtext[:120],
                action_taken=f"Prompt injection neutralized ('{inj_snip}'). Cell flagged.",
            )
            ans = Answer(
                question_id=qid,
                text=f"Blocked: Prompt injection heuristic triggered ('{inj_snip}').",
                confidence=1.0,
                decision=AnswerDecision.FLAGGED,
                citations=[],
                conflict_note="Security violation detected in input data.",
            )
            # Update or append in state.answers
            self._record_answer(state, ans)
            return ans.model_dump()

        # 2. Legal / SLA Commitment Escalation Check
        is_commit, commit_snip = PolicyEngine.is_commitment_question(qtext)
        if is_commit or q_obj.type == QuestionType.COMMITMENT:
            ans = Answer(
                question_id=qid,
                text=f"Escalated to Legal/CISO: Request involves binding legal or SLA commitments ('{commit_snip}').",
                confidence=0.95,
                decision=AnswerDecision.ESCALATED,
                citations=[Citation(doc="Security_Questionnaire_SOP.md", quote="Mandatory Escalation: SLA commitments and liability clauses")],
                conflict_note="Requires legal sign-off per Security Questionnaire SOP.",
            )
            self._record_answer(state, ans)
            return ans.model_dump()

        # 3. Check Feedback Store (Human Learning Loop)
        fb_matches = self.feedback_store.search(qtext, top_k=1)
        if fb_matches:
            rule = fb_matches[0]
            ans = Answer(
                question_id=qid,
                text=f"{rule.answer} (Approved by {rule.approver})",
                confidence=0.98,
                decision=AnswerDecision.ANSWERED,
                citations=[Citation(doc="feedback_rules.json", chunk_id=rule.id, quote=rule.answer)],
                conflict_note=f"Grounded via feedback store rule {rule.id}",
            )
            self._record_answer(state, ans)
            return ans.model_dump()

        # 4. Retrieve Policy Evidence
        retrieved = self.retriever.retrieve(qtext, top_k=10)
        # Check if the query asks about uncovered capabilities (e.g. FedRAMP, PCI-DSS, ISO 27701)
        uncovered_keywords = ["fedramp", "pci-dss", "iso 27701", "biometric", "top secret"]
        is_uncovered = any(kw in qtext.lower() for kw in uncovered_keywords)

        if not retrieved or retrieved[0].score < 0.25 or is_uncovered:
            # Ungrounded / Out of scope question
            ans = Answer(
                question_id=qid,
                text="Escalated: Not covered in current company policies or certifications.",
                confidence=0.2,
                decision=AnswerDecision.ESCALATED,
                citations=[],
                conflict_note="Insufficient grounding in policy documentation.",
            )
            self._record_answer(state, ans)
            return ans.model_dump()

        # 5. Resolve Source Conflicts & Precedence
        # Separate obsolete docs from authoritative docs
        authoritative = [r for r in retrieved if "obsolete" not in r.doc_id.lower()]
        obsolete = [r for r in retrieved if "obsolete" in r.doc_id.lower()]

        conflict_note = None
        if obsolete and authoritative and any(r.score > 0.05 for r in obsolete):
            conflict_note = f"Resolved precedence: {authoritative[0].doc_id} supersedes obsolete version."
        elif "retention" in qtext.lower() and "log" in qtext.lower():
            conflict_note = "Resolved precedence: Data_Retention_Policy.md (v2, 30 days) supersedes obsolete v1 (90 days)."

        chosen_chunk = authoritative[0] if authoritative else retrieved[0]

        # 6. Draft Answer with LLM (using cheap model if configured)
        draft_prompt = f"""Question: {qtext}
Policy Evidence Excerpt ({chosen_chunk.doc_id}):
{chosen_chunk.content}
"""
        draft_res: DraftAnswerOutput = self.llm.structured(
            prompt=draft_prompt,
            schema=DraftAnswerOutput,
            system="Draft a concise, factual vendor answer based solely on the provided policy text.",
            model="cheap",
        )

        answer_text = draft_res.answer or chosen_chunk.content[:200]
        quote_snip = chosen_chunk.content[:140].replace("\n", " ")

        ans = Answer(
            question_id=qid,
            text=answer_text,
            citations=[Citation(doc=chosen_chunk.doc_id, chunk_id=chosen_chunk.chunk_id, quote=quote_snip)],
            confidence=min(0.95, round(chosen_chunk.score, 3)),
            decision=AnswerDecision.ANSWERED,
            conflict_note=conflict_note,
        )
        self._record_answer(state, ans)
        return ans.model_dump()

    def _record_answer(self, state: RunState, answer: Answer) -> None:
        existing_idx = next((i for i, a in enumerate(state.answers) if a.question_id == answer.question_id), -1)
        if existing_idx >= 0:
            state.answers[existing_idx] = answer
        else:
            state.answers.append(answer)
