"""
scripts/demo_reset.py

Resets and prepares the demo environment:
1. Archives the existing demo/ folder to demo_archive/<timestamp>/ (if demo/ exists and is non-empty).
2. Recreates an empty demo/ structure:
   - demo/seed/policies/
   - demo/mailbox/inbox/
   - demo/mailbox/sent/
   - demo/artifacts/
   - demo/kb/
   - demo/inbox/
3. Copies starting seed data:
   - Policy markdown files from data/policies/ (or creates fresh ones) into demo/seed/policies/
   - Acme email .eml files into demo/mailbox/inbox/
   - known_customers.json into demo/known_customers.json
   - Acme questionnaire into demo/inbox/ (for upload/portal flows)
4. Ingests the policies from demo/seed/policies/ into the demo knowledge base (demo/kb/).
5. Is completely idempotent and prints everything it did.
"""
import os
import sys
import shutil
import glob
from datetime import datetime, timezone

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from scripts.generate_data import (
    create_policies,
    create_questionnaire,
    create_test_emails,
    seed_knowledge_base,
)


def reset_demo(demo_dir: str = "demo", archive_base: str = "demo_archive") -> None:
    now_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # 1. Archive current demo/ folder if it exists and has content
    if os.path.exists(demo_dir):
        items = os.listdir(demo_dir)
        if items:
            archive_target = os.path.join(archive_base, now_ts)
            os.makedirs(archive_base, exist_ok=True)
            print(f"[demo_reset] Archiving existing '{demo_dir}' to '{archive_target}'...")
            shutil.move(demo_dir, archive_target)
            print(f"[demo_reset] Successfully archived '{demo_dir}' -> '{archive_target}'.")
        else:
            print(f"[demo_reset] Directory '{demo_dir}' is already empty. Removing for clean recreate...")
            shutil.rmtree(demo_dir)

    # 2. Recreate empty demo structure
    seed_policies_dir = os.path.join(demo_dir, "seed", "policies")
    mailbox_inbox_dir = os.path.join(demo_dir, "mailbox", "inbox")
    mailbox_sent_dir = os.path.join(demo_dir, "mailbox", "sent")
    artifacts_dir = os.path.join(demo_dir, "artifacts")
    kb_dir = os.path.join(demo_dir, "kb")
    inbox_dir = os.path.join(demo_dir, "inbox")

    os.makedirs(seed_policies_dir, exist_ok=True)
    os.makedirs(mailbox_inbox_dir, exist_ok=True)
    os.makedirs(mailbox_sent_dir, exist_ok=True)
    os.makedirs(artifacts_dir, exist_ok=True)
    os.makedirs(kb_dir, exist_ok=True)
    os.makedirs(inbox_dir, exist_ok=True)
    print(f"[demo_reset] Created clean directory structure in '{demo_dir}':")
    print(f"  - {seed_policies_dir}")
    print(f"  - {mailbox_inbox_dir}")
    print(f"  - {mailbox_sent_dir}")
    print(f"  - {artifacts_dir}")
    print(f"  - {kb_dir}")
    print(f"  - {inbox_dir}")

    # 3. Copy/seed starting data
    # (a) Policies into demo/seed/policies
    source_policies = "data/policies"
    if os.path.exists(source_policies) and glob.glob(os.path.join(source_policies, "*.md")):
        print(f"[demo_reset] Copying policy files from '{source_policies}' to '{seed_policies_dir}'...")
        for p in glob.glob(os.path.join(source_policies, "*.md")):
            shutil.copy(p, seed_policies_dir)
    else:
        print(f"[demo_reset] Generating default policy files into '{seed_policies_dir}'...")
        create_policies(seed_policies_dir)

    policy_count = len(glob.glob(os.path.join(seed_policies_dir, "*.md")))
    print(f"[demo_reset] Staged {policy_count} policies in '{seed_policies_dir}'.")

    # (b) Questionnaire in demo/inbox (for email attachments and direct runs)
    create_questionnaire(inbox_dir, "Acme_Security_Questionnaire.xlsx")
    print(f"[demo_reset] Generated questionnaire files in '{inbox_dir}'.")

    # (c) Acme email fixtures in demo/mailbox/inbox
    print(f"[demo_reset] Generating Acme email fixtures in '{mailbox_inbox_dir}'...")
    create_test_emails(mailbox_inbox_dir)
    email_count = len(glob.glob(os.path.join(mailbox_inbox_dir, "*.eml")))
    print(f"[demo_reset] Created {email_count} email messages in '{mailbox_inbox_dir}'.")

    # (d) known_customers.json in demo/known_customers.json
    known_customers_target = os.path.join(demo_dir, "known_customers.json")
    if os.path.exists("data/known_customers.json"):
        shutil.copy("data/known_customers.json", known_customers_target)
        print(f"[demo_reset] Copied 'data/known_customers.json' -> '{known_customers_target}'.")
    else:
        with open(known_customers_target, "w", encoding="utf-8") as f:
            f.write('{\n  "acme": ["acme.com"],\n  "globex": ["globex.com"],\n  "initech": ["initech.com"],\n  "umbrella": ["umbrella.corp", "umbrella.com"]\n}\n')
        print(f"[demo_reset] Created default '{known_customers_target}'.")

    # 4. Ingest policies into knowledge base (demo/kb)
    print(f"[demo_reset] Ingesting policies from '{seed_policies_dir}' into KB '{kb_dir}'...")
    import agent.kb
    agent.kb._kb_service = None  # Reset singleton
    seed_knowledge_base(policies_dir=seed_policies_dir, kb_dir=kb_dir)

    status = agent.kb.kb_status(kb_dir)
    print(f"[demo_reset] Knowledge base indexed {status.get('total_documents', 0)} documents with {status.get('total_chunks', 0)} chunks.")

    print("\n[demo_reset] Demo reset complete and ready for testing or live recording.")
    print(f"Run the Streamlit UI with:\n"
          f"  CHECKPOINT_DB={demo_dir}/runs.sqlite \\\n"
          f"  ARTIFACTS_DIR={demo_dir}/artifacts \\\n"
          f"  MAILBOX_DIR={demo_dir}/mailbox \\\n"
          f"  KB_DIR={demo_dir}/kb \\\n"
          f"  KNOWN_CUSTOMERS_FILE={demo_dir}/known_customers.json \\\n"
          f"  streamlit run app/streamlit_app.py\n")


if __name__ == "__main__":
    reset_demo()
