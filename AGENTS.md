# Codex Instructions — Group Claims Management System

Before changing any code, read and follow **`docs/ENGINEERING_GUARDRAILS.md`** in full. Those rules apply to the entire repository, including subdirectories.

**Approved architecture:** Electron + React/TypeScript + Vite + Python + SQLite + local immutable document copies; single user; macOS/Windows; optional explicitly authorized cloud AI. Use the approved *Group Claims Architecture v2.1* PDF as architectural reference when available. Do not introduce Tauri, Rust, Docker, Redis, hosted storage, multi-user sync, or local AI without user approval.

For **each user request**: (1) inspect current repository/Git status, (2) identify the current milestone and make a minimal plan, (3) implement only that milestone, (4) run checks and report exact results, (5) stop. Never continue into the next milestone without permission.

If the shared guardrails file is absent, pause and request restoration instead of improvising project policies.
