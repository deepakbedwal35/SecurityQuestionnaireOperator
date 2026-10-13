"""
Security policy, commitment detection, and prompt injection heuristics.
"""
import re
from typing import List, Optional, Tuple
from agent.state import QuestionType


# Patterns that indicate binding legal commitments, liability, indemnification, or strict SLA guarantees
COMMITMENT_PATTERNS = [
    r"\buptime\s+guarantee\b",
    r"\b99\.99%?\b",
    r"\bliability\b",
    r"\bindemnif(y|ication|ies)\b",
    r"\bliquidated\s+damages\b",
    r"\bwarrant(y|ies)\b",
    r"\bunlimited\s+liability\b",
    r"\bpenalty\s+clause\b",
    r"\bhold\s+harmless\b",
    r"\bgreater\s+than\s+99\.9%?\b",
]

# Heuristics for detecting prompt injection attempts in untrusted input (PDFs, xlsx cells, web text)
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior)\s+instructions",
    r"system\s*prompt",
    r"answer\s+[\"']?yes[\"']?\s+to\s+all",
    r"you\s+are\s+now\s+in\s+developer\s+mode",
    r"bypass\s+all\s+(checks|rules|policies)",
    r"reveal\s+secret",
    r"output\s+the\s+hidden\s+token",
    r"<script[\s>]",
    r"javascript:",
    r"DROP\s+TABLE",
]


def detect_prompt_injection(text: str, source: str = "untrusted") -> Optional["InjectionFlag"]:
    from agent.state import InjectionFlag
    is_inj, snippet = PolicyEngine.detect_prompt_injection(text)
    if is_inj:
        return InjectionFlag(
            source=source,
            snippet=snippet or "prompt injection detected",
            action_taken="Flagged and neutralized",
        )
    return None


class PolicyEngine:
    @staticmethod
    def detect_prompt_injection(text: str) -> Tuple[bool, Optional[str]]:
        """
        Analyzes untrusted text for prompt injection markers.
        Returns (is_injection, matched_pattern).
        """
        if not text:
            return False, None
        
        normalized = text.lower()
        for pattern in INJECTION_PATTERNS:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                return True, match.group(0)
        return False, None

    @staticmethod
    def is_commitment_question(text: str) -> Tuple[bool, Optional[str]]:
        """
        Identifies whether a question demands legal or high-stakes SLA commitment.
        Returns (is_commitment, matched_topic).
        """
        if not text:
            return False, None
        
        normalized = text.lower()
        for pattern in COMMITMENT_PATTERNS:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                return True, match.group(0)
        return False, None

    @staticmethod
    def classify_question(text: str) -> Tuple[QuestionType, List[str]]:
        """
        Classifies a question into factual, commitment, or out_of_scope,
        returning any detected risk flags.
        """
        flags: List[str] = []
        is_inj, inj_snippet = PolicyEngine.detect_prompt_injection(text)
        if is_inj:
            flags.append(f"prompt_injection_detected: {inj_snippet}")

        is_commit, commit_snippet = PolicyEngine.is_commitment_question(text)
        if is_commit:
            flags.append(f"commitment_required: {commit_snippet}")
            return QuestionType.COMMITMENT, flags

        return QuestionType.FACTUAL, flags

    @staticmethod
    def sanitize_untrusted_text(text: str) -> str:
        """
        Sanitizes text extracted from xlsx/PDF/web before passing into model context.
        Encapsulates and neutralizes control character exploits.
        """
        if not text:
            return ""
        # Strip ASCII control characters except newline and tab
        sanitized = "".join(ch for ch in text if ch in "\n\t" or 32 <= ord(ch) <= 126 or ord(ch) > 127)
        return sanitized.strip()

