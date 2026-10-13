"""
Command-line interface to run the full agent loop (understand -> plan -> execute -> verify -> report).
Supports:
  --llm fake (default) | real
  --verbose
"""
import argparse
import os
import sys
import time

from agent.graph import create_agent_graph
from agent.llm import get_llm_client
from agent.memory import FeedbackStore
from agent.state import AnswerDecision, RunState, RunStatus
from agent.tools.composite import ProcessQuestionTool
from agent.tools.files import FindInboxFilesTool, ParseQuestionnaireTool, WriteFilledXlsxTool
from agent.tools.human import RequestHumanTool
from agent.tools.registry import ToolRegistry
from agent.tools.retrieve import KeywordPolicyRetriever, RetrieveEvidenceTool


def setup_registry(retriever: KeywordPolicyRetriever, feedback_store: FeedbackStore, llm_client) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(FindInboxFilesTool())
    registry.register(ParseQuestionnaireTool())
    registry.register(WriteFilledXlsxTool())
    registry.register(RetrieveEvidenceTool(retriever=retriever, feedback_store=feedback_store))
    registry.register(ProcessQuestionTool(retriever=retriever, llm_client=llm_client, feedback_store=feedback_store))
    registry.register(RequestHumanTool())
    return registry


def main():
    parser = argparse.ArgumentParser(description="Security Questionnaire Operator CLI")
    parser.add_argument("goal", nargs="?", default="Acme sent us a security questionnaire, get it done.")
    parser.add_argument("--llm", default="fake", choices=["fake", "real", "anthropic"], help="LLM provider to use")
    parser.add_argument("--verbose", action="store_true", help="Print verbose step logs and observations")
    args = parser.parse_args()

    provider = "groq" if args.llm == "real" else args.llm
    llm_client = get_llm_client(provider)

    # CLI preflight check
    try:
        llm_client.validate_configuration()
    except Exception as e:
        print(f"\n[ERROR] Configuration check failed: {e}")
        sys.exit(1)

    feedback_store = FeedbackStore(file_path="data/feedback_rules.json")
    retriever = KeywordPolicyRetriever(policies_dir="data/policies", feedback_store=feedback_store)
    registry = setup_registry(retriever, feedback_store, llm_client)

    graph = create_agent_graph(llm_client, registry, retriever)

    run_id = f"run-{int(time.time())}"
    state = RunState(
        run_id=run_id,
        request_text=args.goal,
        status=RunStatus.RUNNING,
    )

    print(f"\n=======================================================")
    print(f" SECURITY QUESTIONNAIRE OPERATOR")
    print(f" Run ID: {run_id}")
    print(f" LLM Provider: {provider.upper()}")
    print(f" Goal: \"{args.goal}\"")
    print(f"=======================================================\n")

    result_dict = graph.invoke(state)
    final_state: RunState = result_dict if isinstance(result_dict, RunState) else RunState(**result_dict)

    if args.verbose:
        print("\n--- STEP EXECUTION LOG ---")
        for log in final_state.observations:
            print(f"[{log.tool}] Risk: {log.risk.value} | Result: {log.output_summary} ({log.duration_ms}ms)")

    answered_count = sum(1 for a in final_state.answers if a.decision == AnswerDecision.ANSWERED)
    escalated_count = sum(1 for a in final_state.answers if a.decision == AnswerDecision.ESCALATED)
    flagged_count = sum(1 for a in final_state.answers if a.decision == AnswerDecision.FLAGGED)

    print("\n--- EXECUTION SUMMARY ---")
    print(f"  Status: {final_state.status.value.upper()}")
    print(f"  Customer: {final_state.understood_goal.customer if final_state.understood_goal else 'N/A'}")
    print(f"  Chosen File: {final_state.understood_goal.questionnaire_path if final_state.understood_goal else 'N/A'}")
    print(f"  Selection Rationale: {final_state.understood_goal.selection_rationale if final_state.understood_goal else 'N/A'}")
    print(f"  Total Questions: {len(final_state.questions)}")
    print(f"  Direct Answers: {answered_count}")
    print(f"  Escalations (Sign-off Needed): {escalated_count}")
    print(f"  Security Injections Flagged: {flagged_count}")
    print(f"  Total Steps: {final_state.counters.step_count} (Errors: {final_state.counters.tool_error_count})")
    print(f"  Replans: {final_state.counters.replan_count} (Plan Version: {final_state.plan_version})")

    print("\n--- MACHINE VERIFICATION RESULTS ---")
    for pr in final_state.postcondition_results:
        status_txt = "PASSED" if pr.passed else "FAILED"
        print(f"  [{status_txt}] {pr.kind}: {pr.evidence}")

    print("\n--- ARTIFACTS GENERATED ---")
    for k, v in final_state.artifacts.items():
        print(f"  - {k}: {v}")

    print(f"\nReport ready at: {final_state.artifacts.get('report_md')}")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
