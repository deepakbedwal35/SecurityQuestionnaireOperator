"""
Evaluation runner stub for the 6 core scenarios.
Runs each scenario against the agent, verifies postconditions and outputs a metrics summary table.
"""
import sys
from evals.scenarios import SCENARIOS


def run_all_evals():
    print("=" * 70)
    print("Security Questionnaire Operator - Scenario Evaluation Runner")
    print("=" * 70)
    print(f"Loaded {len(SCENARIOS)} scenarios:")
    for sc in SCENARIOS:
        print(f" - [{sc.id}] {sc.name}: {sc.description}")

    print("\n[NOTE] Full automated scenario eval suite will run end-to-end once graph and portal are connected.")
    print("=" * 70)


if __name__ == "__main__":
    run_all_evals()

