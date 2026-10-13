"""
Feedback memory store for recording human approvals/corrections and augmenting retrieval.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeedbackRule(BaseModel):
    id: str
    question: str
    answer: str
    approver: str
    timestamp: datetime = Field(default_factory=_utcnow)
    category: Optional[str] = None
    notes: Optional[str] = None


class FeedbackStore:
    def __init__(self, file_path: Optional[str] = None) -> None:
        from agent.config import get_feedback_store_path
        self.file_path = file_path or get_feedback_store_path()
        self._ensure_store()

    def _ensure_store(self) -> None:
        if not os.path.exists(self.file_path):
            os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    def load_rules(self) -> List[FeedbackRule]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [FeedbackRule(**item) for item in data]
        except Exception:
            return []

    def save_rule(self, rule: FeedbackRule) -> None:
        rules = self.load_rules()
        # Replace if ID exists, else append
        existing_idx = next((i for i, r in enumerate(rules) if r.id == rule.id), -1)
        if existing_idx >= 0:
            rules[existing_idx] = rule
        else:
            rules.append(rule)

        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([r.model_dump(mode="json") for r in rules], f, indent=2)

    def search(self, query: str, top_k: int = 3) -> List[FeedbackRule]:
        """Simple keyword/token overlap search against feedback questions."""
        rules = self.load_rules()
        if not rules or not query:
            return []

        query_tokens = set(re.findall(r"\w+", query.lower()))
        scored: List[tuple[int, FeedbackRule]] = []
        for rule in rules:
            rule_tokens = set(re.findall(r"\w+", rule.question.lower()))
            overlap = len(query_tokens.intersection(rule_tokens))
            if overlap > 0:
                scored.append((overlap, rule))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

