# Mock Customer Portal

Server-rendered Express application that simulates a customer security questionnaire web portal.

## Features
- **Semantic HTML**: Form inputs tied to accessible `<label for="...">` with predictable accessible roles.
- **Form Controls**: Text areas, Yes/No select dropdowns, single-choice dropdowns matching `Q-01` through `Q-40`.
- **Dynamic Conditionals**: 3 conditional follow-ups:
  - `Q-01` (MFA) -> `Q-01a` (Specific factors supported)
  - `Q-18` (SOC) -> `Q-18a` (SOC contact hotline)
  - `Q-21` (Multi-region cloud) -> `Q-21a` (Primary and secondary regions)
- **Draft Persistence**: Save drafts per session user.
- **Idempotency & Duplicate Protection**: Submission tokens and duplicate detection returning existing `SUB-xxxxx` IDs.
- **Independent Truth Grounding**: `/submissions` lists all stored answers for verification.
- **Fault Injection**: Supports `PORTAL_FAULTS` env and runtime `/__admin/faults` endpoint:
  - `session_expiry`: Session expires mid-run.
  - `ui_rename`: Button/input labels change dynamically.
  - `slow`: Random 1-3s delays.
  - `flaky_save`: First draft save fails with 500, then succeeds.
  - `injection_banner`: Prompt injection banner on form.
  - `duplicate`: Pre-existing submission collision.

## Running
```bash
PORT=4000 node server.js
```
Or use the Python test manager:
```bash
python3 scripts/start_portal.py
```

