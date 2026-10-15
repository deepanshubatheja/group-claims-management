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
standard library. The Dashboard supports native file/folder selection and drag-and-drop. Drop paths are
available only inside the preload/main-process boundary; the renderer receives a bounded import preview,
then the existing explicit confirmation imports supported local files.

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

## Phase 6.2A OpenAI provider dependency

The Python-only OpenAI adapter uses the official `openai` SDK, declared in
`backend/pyproject.toml`. Install all backend dependencies with
`backend/.venv/bin/python -m pip install -e backend` (on Windows, use the
virtual-environment interpreter). For development only, export
`OPENAI_API_KEY` in the terminal that starts Electron; optionally set
`GROUP_CLAIMS_AI_PROVIDER=openai` and `GROUP_CLAIMS_AI_MODEL=gpt-4.1-mini`.
Never put credentials in source files, SQLite, renderer code, or committed
`.env` files. The adapter does not test credentials or send content on startup.
Phase 6.2B adds document-scoped analysis jobs. It transmits only bounded,
evidence-referenced deterministic extraction fragments after both global Settings
authorization and a deliberate per-document Analyze action. Original vault files
are never uploaded by default. The current development environment uses
`OPENAI_API_KEY`; packaged per-user OS-protected credential storage remains a
future distribution requirement.

If Settings reports that the key is not detected, use **Refresh AI
configuration**. This is a boolean-only report from the Python backend: it
never displays, logs, stores, or sends the key through IPC. Export the key in
the *same terminal* before running `npm run dev`, then stop the existing
development processes and start them again. A shell variable that was not
exported, or a key added after Electron and its Python child were launched,
cannot be inherited by that child.

"Key detected" means only that the Python backend inherited a non-empty value;
it does not validate that OpenAI will accept it. If an analysis reports
`AI_AUTHENTICATION_FAILED`, stop the running app, replace the terminal value
with an active API key for the intended OpenAI project (do not include quote
characters in the key itself), and restart `npm run dev`. Do not repeatedly
retry a rejected key. The application trims accidental surrounding whitespace,
but never displays, logs, persists, or transmits the key through IPC.

### Explicit OpenAI connection check (development)

From the same macOS Terminal that will start the application, set the values for
the current shell session, then start the app from that terminal. To avoid
echoing the key while it is entered in zsh:

```zsh
read -r -s "OPENAI_API_KEY?Paste OpenAI project API key, then press Return: "
printf '\n'
export OPENAI_API_KEY
export GROUP_CLAIMS_AI_PROVIDER=openai
export GROUP_CLAIMS_AI_MODEL=gpt-4.1-mini
npm run dev
```

After Electron starts, open **Settings → AI Processing** and first use
**Refresh AI configuration**. It reports only whether the running Python child
received a non-empty key. If it says the key is present, **Check API
Connection** is a separate, deliberate request to OpenAI's Models metadata
endpoint; it sends no document content and creates no model response. It may
still be subject to provider availability and account/project permissions. A
connection-check result describes that explicit request only: `Key present` is
not proof of authentication. The UI reports controlled authentication,
permission, rate-limit, network, timeout, or generic provider errors without
revealing provider response bodies, headers, or credentials.

On Windows PowerShell, set the process environment variable in the same window
before `npm run dev`, for example `$env:OPENAI_API_KEY = "..."`, then set the
two `GROUP_CLAIMS_*` variables similarly. Do not place a key in `package.json`,
source code, SQLite, or a committed `.env` file. Stop all existing `npm run dev`
processes before changing environment values: an already-running Electron/Python
process cannot inherit a later shell export.

## Phase 6.2D development AI cost controls

AI analysis is never started by import, document selection, or backend startup.
For the first explicitly initiated OpenAI test, the safe defaults are one
document and one request, 4,000 full-request input characters, 1,500 estimated
input tokens, 1,000 output tokens, zero automatic retries, and a USD 0.01
per-analysis estimate limit. The backend sets SDK retries to zero and only
permits application retries when a developer explicitly sets a higher bounded
`GROUP_CLAIMS_AI_MAX_REQUEST_ATTEMPTS` value.

Before transmission, input tokens are conservatively estimated as one token per
three full request characters (including instructions); the maximum output token
limit is included in the cost calculation. The bundled pricing profile applies
only to `gpt-4.1-mini`: USD 0.40/M input and USD 1.60/M output tokens. For
another model, configure both `GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION` and
`GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION`, based on the current official pricing,
or the backend will block the request. The Documents UI shows planned/excluded
coverage and an estimated—not actual billed—cost before the user can authorize
the request. It does not fetch or claim to know an OpenAI account balance.

### Consolidated Matter Review analysis

Matter Review can prepare an explicitly authorized, bounded batch for up to
20 already parsed documents. It never runs when documents are imported. The
screen first shows the per-document scope and aggregate estimate; after the
user checks the batch-specific consent control, the backend runs at most one
request per selected document sequentially, with no automatic retries. The
aggregate development ceiling defaults to USD 0.20 and can be reduced with:

```zsh
export GROUP_CLAIMS_AI_MAX_BATCH_ESTIMATED_COST_USD=0.20
```

The individual per-document limits still apply. A batch stops new transfers if
global authorization is withdrawn; completed document analyses are preserved.

After completed analyses are consolidated, Matter Review also offers a
separate **Consolidated Q&A** action. It sends the user's question plus a
bounded set of evidence-linked findings from the currently selected documents,
never original vault files. It requires a new per-question consent checkbox,
uses the existing per-question cost limit and no automatic retries, and keeps
questions and answers only in the running backend's memory.

### Document Q&A

The lawyer-facing Documents workspace deliberately hides the raw parser grid
and PDF text. Deterministic extraction remains stored locally for provenance;
AI findings and cited source excerpts are shown instead. As a question is typed,
the application calculates its bounded scope and estimate locally; this does
not contact OpenAI. **Ask AI** is enabled only after global cloud authorization
and a fresh per-question confirmation. Each question is a separate request,
with no automatic retries and a maximum of 500 output tokens. The Python backend
sends only the question, current evidence-validated findings, and bounded
deterministic source excerpts; it never sends vault paths or whole original
files by default. Questions and answers are kept in memory for the running
backend session only; they are not logged or saved to SQLite.

## Naming and organization

- Use TypeScript for Electron and frontend code, with explicit types at process boundaries.
- Use Python type hints for backend code.
- Prefer clear, domain-oriented names; keep UI, desktop-boundary, and domain logic separate.
- Keep modules focused and avoid speculative abstractions.
- Document approved significant design decisions in `docs/DECISIONS.md` when they arise.

## Cross-platform considerations

Use platform-neutral path APIs and test macOS and Windows path behavior. User data, local databases, immutable imported-document copies, exports, backups, and logs must live in an OS-standard per-user application-data directory outside the repository and install location. Do not assume case-sensitive paths, POSIX-only behavior, or a globally installed Python runtime in packaged builds.
