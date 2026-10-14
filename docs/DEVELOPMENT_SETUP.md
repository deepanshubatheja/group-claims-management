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

Phase 4 storage is initialized by the Python backend from Electron's
`app.getPath('userData')` value. On macOS this is normally under
`~/Library/Application Support/Group Claims Management`; on Windows it is under
the current user's `%APPDATA%\\Group Claims Management`. The repository and app
install directory never contain user documents or runtime databases. The managed
layout is `database/`, `documents/`, `exports/`, `backups/`, and `logs/`.
Local storage is not encrypted automatically.

Run Python protocol tests with `cd backend && ./.venv/bin/python -m unittest discover -s tests -v`
on macOS/Linux, or the corresponding `.venv\\Scripts\\python.exe` command on Windows.

## Phase 5 document parsing dependencies

Install the declared backend parser dependencies into the project virtual environment with
`cd backend && ./.venv/bin/python -m pip install -e .`. They provide deterministic XLSX
(`openpyxl`), legacy XLS (`xlrd`), coordinate-aware PDF native text (`PyMuPDF`, with `pypdf`
retained for compatibility), and local OCR handoff (`Pillow`, `pytesseract`); CSV uses Python's
standard library. Native file selection is the supported import path. Drag-and-drop remains deliberately
disabled until its Electron file-handle handoff can be verified without exposing paths to the renderer.

## Phase 5.2 local OCR

Tesseract is a separate local runtime; the Python package does not install it. In development,
install Tesseract and its required language data with the platform's normal package manager, then
verify it with `tesseract --list-langs`. English (`eng`) is required for OCR; German (`deu`) is used
when installed. On Windows, a future packaged app must bundle a reviewed Tesseract runtime and the
selected `tessdata`; end users must not need to install Python or Tesseract.

The parser always tries native PDF text first. It renders and OCRs only pages without a usable text
layer, sequentially with page, pixel, and per-page-time limits. OCR stays local, stores page-level
provenance and a quality warning, and must be reviewed against the immutable source PDF. If the
runtime or language data is unavailable, the document reports a controlled OCR-unavailable result
instead of a false successful parse.

## Naming and organization

- Use TypeScript for Electron and frontend code, with explicit types at process boundaries.
- Use Python type hints for backend code.
- Prefer clear, domain-oriented names; keep UI, desktop-boundary, and domain logic separate.
- Keep modules focused and avoid speculative abstractions.
- Document approved significant design decisions in `docs/DECISIONS.md` when they arise.

## Cross-platform considerations

Use platform-neutral path APIs and test macOS and Windows path behavior. User data, local databases, immutable imported-document copies, exports, backups, and logs must live in an OS-standard per-user application-data directory outside the repository and install location. Do not assume case-sensitive paths, POSIX-only behavior, or a globally installed Python runtime in packaged builds.
