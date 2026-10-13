"""
Configuration helper for SecQ Operator.
Centralizes environment variable defaults so all storage, mailbox, database,
and artifact directories can be configured and overridden dynamically.
"""
import os


def get_checkpoint_db() -> str:
    return os.environ.get("CHECKPOINT_DB", "data/runs.sqlite")


def get_artifacts_dir() -> str:
    return os.environ.get("ARTIFACTS_DIR", "artifacts")


def get_mailbox_dir() -> str:
    return os.environ.get("MAILBOX_DIR", "data/mailbox")


def get_inbox_mailbox_dir() -> str:
    return os.path.join(get_mailbox_dir(), "inbox")


def get_sent_mailbox_dir() -> str:
    return os.path.join(get_mailbox_dir(), "sent")


def get_kb_dir() -> str:
    return os.environ.get("KB_DIR", "data/kb")


def get_known_customers_file() -> str:
    return os.environ.get("KNOWN_CUSTOMERS_FILE", "data/known_customers.json")


def get_policies_dir() -> str:
    return os.environ.get("POLICIES_DIR", "data/policies")


def get_feedback_store_path() -> str:
    return os.environ.get("FEEDBACK_STORE", os.environ.get("FEEDBACK_STORE_PATH", "data/feedback_rules.json"))


def get_storage_backend() -> str:
    return os.environ.get("STORAGE_BACKEND", "local").lower()

