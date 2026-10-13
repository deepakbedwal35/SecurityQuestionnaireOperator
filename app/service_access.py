"""
Singleton service accessor for Streamlit UI.
Configures OperatorService using environment variables and caches instance.
No agent logic or direct SQLite access is performed here.
"""
import os
import streamlit as st
from agent.llm import FakeLLM, GroqLLM
from agent.service import OperatorService

_CACHED_SERVICES = {}


def get_operator_service(mode: str = "fake") -> OperatorService:
    db_path = os.environ.get("CHECKPOINT_DB", "data/runs.sqlite")
    fb_path = os.environ.get("FEEDBACK_STORE", "data/feedback_rules.json")
    policies_dir = "data/policies"

    cache_key = (os.path.abspath(db_path), mode)
    if cache_key in _CACHED_SERVICES:
        return _CACHED_SERVICES[cache_key]

    llm = FakeLLM() if mode == "fake" else GroqLLM()
    svc = OperatorService(
        db_path=db_path,
        feedback_path=fb_path,
        policies_dir=policies_dir,
        llm_client=llm,
    )
    _CACHED_SERVICES[cache_key] = svc
    return svc
