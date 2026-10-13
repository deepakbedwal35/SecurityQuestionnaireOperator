"""
Root test configuration and autouse fixtures.
Ensures tests never mutate real data or real artifacts by redirecting
all database, mailbox, artifact, and KB paths to isolated temporary directories.
"""
import os
import shutil
import pytest

from agent.config import get_kb_dir, get_policies_dir
from scripts.generate_data import (
    create_gold_answers,
    create_policies,
    create_questionnaire,
    create_test_emails,
    seed_knowledge_base,
)


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path, monkeypatch):
    """
    Autouse fixture that runs before each test.
    Points all configurable storage, mailbox, database, and knowledge-base paths
    to subdirectories of pytest's tmp_path.
    """
    # 1. Prepare isolated paths
    db_path = str(tmp_path / "runs.sqlite")
    artifacts_dir = str(tmp_path / "artifacts")
    mailbox_dir = str(tmp_path / "mailbox")
    inbox_dir = str(tmp_path / "mailbox" / "inbox")
    sent_dir = str(tmp_path / "mailbox" / "sent")
    kb_dir = str(tmp_path / "kb")
    policies_dir = str(tmp_path / "policies")
    feedback_path = str(tmp_path / "feedback_rules.json")
    known_customers_path = str(tmp_path / "known_customers.json")
    inbox_files_dir = str(tmp_path / "inbox")
    output_files_dir = str(tmp_path / "output")

    os.makedirs(artifacts_dir, exist_ok=True)
    os.makedirs(inbox_dir, exist_ok=True)
    os.makedirs(sent_dir, exist_ok=True)
    os.makedirs(kb_dir, exist_ok=True)
    os.makedirs(policies_dir, exist_ok=True)
    os.makedirs(inbox_files_dir, exist_ok=True)
    os.makedirs(output_files_dir, exist_ok=True)

    # 2. Configure environment variables via monkeypatch
    monkeypatch.setenv("CHECKPOINT_DB", db_path)
    monkeypatch.setenv("ARTIFACTS_DIR", artifacts_dir)
    monkeypatch.setenv("MAILBOX_DIR", mailbox_dir)
    monkeypatch.setenv("KB_DIR", kb_dir)
    monkeypatch.setenv("POLICIES_DIR", policies_dir)
    monkeypatch.setenv("FEEDBACK_STORE", feedback_path)
    monkeypatch.setenv("FEEDBACK_STORE_PATH", feedback_path)
    monkeypatch.setenv("KNOWN_CUSTOMERS_FILE", known_customers_path)
    monkeypatch.setenv("STORAGE_BACKEND", "local")

    # 3. Seed test data into tmp_path
    create_policies(policies_dir)
    create_questionnaire(inbox_files_dir)
    # Also copy known customers
    src_customers = "data/known_customers.json"
    if os.path.exists(src_customers):
        shutil.copy(src_customers, known_customers_path)
    else:
        with open(known_customers_path, "w", encoding="utf-8") as f:
            f.write('{"acme": ["acme.com"], "globex": ["globex.com"], "initech": ["initech.com"]}')

    # Seed test emails into mailbox inbox
    create_test_emails(inbox_dir)

    # Seed knowledge base from policies
    seed_knowledge_base(policies_dir, kb_dir)

    # Reset any cached singleton services
    import agent.kb
    agent.kb._kb_service = None

    yield

    # Clean up singleton after test
    agent.kb._kb_service = None

