# Development Setup

## Confirmed stack

The application will use Electron, React, TypeScript, Vite, Python, and SQLite. It is a single-user, local-first desktop application for macOS and Windows.

## Directory responsibilities

- `frontend/` — React and TypeScript renderer code only: interface and display state. It must not access the filesystem, SQLite, secrets, billing logic, or external AI providers directly.
- `electron/` — Electron main process and secure preload code: app lifecycle, native dialogs, validated IPC, and Python-core lifecycle management.
- `backend/` — Python application core: domain rules, imports, validation, reconciliation, billing, audit, and SQLite access.
- `docs/` — Project instructions, architectural notes, decisions, setup guidance, and operational documentation.

## Development environment

- A supported Node.js runtime and package manager for Electron, React, TypeScript, and Vite work.
- Python 3 for the backend core and isolated backend dependencies.
- SQLite for local development and verification.
- Git for source control.
- macOS or Windows for supported desktop behavior.

For the Phase 3 backend, create a project-local virtual environment with
`python3 -m venv backend/.venv` (on Windows, use the installed Python launcher
or interpreter). Phase 3 has no third-party Python runtime dependencies.

## Dependency management

Frontend and Electron dependencies will be declared in the repository's Node package manifest and installed through a lockfile-backed package-manager workflow. Backend dependencies will be declared in a Python dependency manifest and installed into a project-local virtual environment. No dependencies are installed during Phase 1.

The Phase 3 Electron-to-Python boundary is private newline-delimited UTF-8 JSON
over the Python child process's stdin/stdout; it does not open a network port.
Electron starts the development backend from `backend/.venv` and exposes only
allowlisted, typed IPC methods to the renderer. Production packaging will bundle
a standalone backend executable in a later milestone, so end users will not need
to install Python.

Run Python protocol tests with `cd backend && ./.venv/bin/python -m unittest discover -s tests -v`
on macOS/Linux, or the corresponding `.venv\\Scripts\\python.exe` command on Windows.

## Naming and organization

- Use TypeScript for Electron and frontend code, with explicit types at process boundaries.
- Use Python type hints for backend code.
- Prefer clear, domain-oriented names; keep UI, desktop-boundary, and domain logic separate.
- Keep modules focused and avoid speculative abstractions.
- Document approved significant design decisions in `docs/DECISIONS.md` when they arise.

## Cross-platform considerations

Use platform-neutral path APIs and test macOS and Windows path behavior. User data, local databases, immutable imported-document copies, exports, backups, and logs must live in an OS-standard per-user application-data directory outside the repository and install location. Do not assume case-sensitive paths, POSIX-only behavior, or a globally installed Python runtime in packaged builds.
