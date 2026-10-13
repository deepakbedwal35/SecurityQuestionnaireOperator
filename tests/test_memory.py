import os
import tempfile
import pytest
from agent.memory import FeedbackRule, FeedbackStore


def test_feedback_store_save_and_load():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        store_path = tf.name

    try:
        store = FeedbackStore(file_path=store_path)
        rule = FeedbackRule(
            id="RULE-01",
            question="Does Northwind support FedRAMP High?",
            answer="Northwind is FedRAMP Moderate In-Process under agency sponsorship.",
            approver="ciso@northwind.io",
        )
        store.save_rule(rule)

        loaded = store.load_rules()
        assert len(loaded) == 1
        assert loaded[0].id == "RULE-01"
        assert loaded[0].approver == "ciso@northwind.io"

        # Search matching query
        results = store.search("Do you support FedRAMP certification?")
        assert len(results) == 1
        assert results[0].id == "RULE-01"

        # Search non-matching query
        no_match = store.search("Random irrelevant topic")
        assert len(no_match) == 0
    finally:
        if os.path.exists(store_path):
            os.remove(store_path)

