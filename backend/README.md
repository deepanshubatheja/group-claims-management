# Python backend development

The backend is a private local child process. It communicates only through newline-delimited UTF-8 JSON over standard input/output; it does not bind an HTTP port.

Electron supplies the trusted per-user application-data path with `--storage-root`.
The backend validates that it is an absolute, non-root, non-symlink directory and
creates `database/`, `documents/`, `exports/`, `backups/`, and `logs/` beneath it.
SQLite is stored at `database/claims.sqlite`. Local files are not automatically encrypted.

## Development setup

From the repository root, create the project-local virtual environment:

```sh
python3 -m venv backend/.venv
```

No third-party Python dependencies are required in Phase 3. Activate the environment if you want to run Python commands directly:

```sh
# macOS / Linux
source backend/.venv/bin/activate

# Windows PowerShell
backend\\.venv\\Scripts\\Activate.ps1
```

Run the backend by itself from its directory:

```sh
cd backend
./.venv/bin/python -m group_claims_backend
```

On Windows, use `.venv\\Scripts\\python.exe` instead. The Electron main process resolves this interpreter per platform during development. Future installers will bundle a standalone backend executable; they will not require a user-managed Python installation.

## Protocol

Each request and response is one UTF-8 JSON object per line. The protocol version is `1.0`, requests are limited to 65,536 bytes, and the allowed commands are `health.check` and `storage.status`.

```json
{"protocolVersion":"1.0","id":"health-1","command":"health.check","params":{}}
```

Protocol messages are written exclusively to stdout. Operational logs use stderr and never include raw request content.

## SQLite and file vault

Migrations are ordered Python functions recorded in `schema_migrations` and are
applied inside explicit SQLite transactions with foreign keys enabled. A failed
migration does not reset or replace an existing database.

`FileVaultService` is backend-only and has no renderer command in this phase. It
accepts an explicitly authorized future import path, rejects links and non-regular
files, streams a SHA-256 hash while copying into `documents/<generated-id>`, and
records metadata transactionally. An identical hash reuses the existing document
record and does not create another managed copy. The original file is never
modified or executed.

## Tests

From the repository root:

```sh
cd backend && ./.venv/bin/python -m unittest discover -s tests -v
```

On Windows:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Phase 6.2 OpenAI provider and document-analysis setup

For the current desktop build, the default provider is Codex. Settings lets you
switch between **Codex (ChatGPT sign-in)** and **OpenAI API key**. The choice is
saved as `ai-provider.json` in the per-user ClaimFlow app-data directory; it
takes precedence over `GROUP_CLAIMS_AI_PROVIDER`, which remains a development
fallback when no choice has been saved. Switching providers turns Cloud AI
authorization off, so enable it again for the newly selected provider. Each
document analysis or question still requires its own explicit action and sends
bounded extracted text, never the original file. The Codex route uses the
pinned `openai-codex` SDK and the OS keyring; no OpenAI API key is
needed for this route. Your ChatGPT plan's Codex usage limits apply. No USD
API cost estimate or output-token cap is available for Codex. Browser login
requires a working localhost callback and may be restricted by your account
or workspace policy.

This login method is for local single-user/internal use only. A commercial or
hosted distribution needs the official Sign in with ChatGPT flow instead of
app-server managed authentication.

The OpenAI provider is installed only in the backend virtual environment and is
never called during backend startup. Install declared dependencies from the
repository root:

```sh
backend/.venv/bin/python -m pip install -e backend
```

For a local development session without a saved Settings choice, supply
credentials through the process environment (or an OS secret manager that
exports it), never source code, SQLite, or Electron configuration:

```sh
export OPENAI_API_KEY="your-key"
export GROUP_CLAIMS_AI_PROVIDER="openai"
export GROUP_CLAIMS_AI_MODEL="gpt-4.1-mini" # optional; this is the default
```

The OpenAI provider is pinned to `https://api.openai.com/v1`; it deliberately
does not honor `OPENAI_BASE_URL`, so a stray shell proxy setting cannot redirect
an authorized document fragment or API credential. The official SDK still
supports organization/project routing for a legacy user key, but Group Claims
uses the explicit `GROUP_CLAIMS_OPENAI_ORG_ID` and
`GROUP_CLAIMS_OPENAI_PROJECT_ID` names instead of inheriting unrelated SDK shell
settings. Project API keys normally do not need either variable. If OpenAI returns an authentication failure, a
non-empty key reached the provider: create or copy an active OpenAI Platform
project key and launch `npm run dev` from that same terminal. Do not paste the
key into the application, source code, or chat.

The backend reports whether the provider is not configured, missing a key, or
configured but not live-tested. It does not contact OpenAI merely to check this
state.

Phase 6.2B adds an explicit, document-scoped workflow. The user must first
enable cloud-processing preparation in Settings, then deliberately choose
**Analyze with AI** for one successfully parsed document. The backend sends only
bounded deterministic extraction fragments (not the original vault file), using
`store=False`, and records non-sensitive authorization and job metadata. A
withdrawal prevents later chunks from being sent; it does not claim to erase a
previous provider request. Findings remain provisional, evidence-validated
proposals and never populate confirmed holdings, claims, or billing tables.
Each analysis job also records non-sensitive validation counts: proposals
returned, schema-valid, evidence-validated, persisted, and rejected by a
controlled reason code. Raw provider responses and document payloads are never
retained. New CSV parses use stable `CSV!A1`-style references; older CSV
extractions remain evidence-resolvable by their stored row and column position.
AI source labels are displayed in square brackets in prompts; the backend
accepts only that outer display wrapper (and surrounding whitespace), then
stores the exact canonical source identifier after local evidence validation.
It does not translate or accept invented coordinates.
The provider rejects a malformed response envelope, but an invalid individual
proposal is counted and discarded during local validation so it cannot discard
an otherwise complete analysis job.

### Development cost controls

Before a document-level authorization can start an OpenAI request, the backend
creates a bounded plan and rejects it if its **estimated** cost exceeds the
development limit. The defaults are intentionally small: one selected document,
one request, at most 4,000 input characters / 1,500 estimated input tokens,
1,000 output tokens, no automatic retry, and a USD 0.01 estimated-cost ceiling.
The provider also sets the OpenAI SDK's `max_retries=0`.

The input estimate is deliberately conservative: `ceil(full request
characters / 3)`, including application instructions, plus the configured
maximum output tokens. For `gpt-4.1-mini`, the checked-in development pricing
profile is USD 0.40 per million input tokens and USD 1.60 per million output
tokens. It is an estimate only, not a statement of account billing, tax, or a
live balance. A different model is blocked until both price values are supplied
explicitly.

The following environment variables are optional, should be set only in the
terminal that launches Electron, and must never be committed:

```sh
export GROUP_CLAIMS_AI_MAX_CHUNKS=1
export GROUP_CLAIMS_AI_MAX_INPUT_CHARACTERS=4000
export GROUP_CLAIMS_AI_MAX_INPUT_TOKENS=1500
export GROUP_CLAIMS_AI_MAX_OUTPUT_TOKENS=1000
export GROUP_CLAIMS_AI_MAX_ESTIMATED_COST_USD=0.01
export GROUP_CLAIMS_AI_MAX_REQUEST_ATTEMPTS=1
# Optional aggregate cap for one explicitly authorized Matter Review batch.
# The default is USD 0.20 and a batch remains capped at 20 documents.
export GROUP_CLAIMS_AI_MAX_BATCH_ESTIMATED_COST_USD=0.20
# Required for a non-default model's explicit local cost estimate:
export GROUP_CLAIMS_AI_INPUT_USD_PER_MILLION=0.40
export GROUP_CLAIMS_AI_OUTPUT_USD_PER_MILLION=1.60
```

Increasing chunks or request attempts increases potential cost and is a
deliberate developer opt-in. The Documents screen shows planned versus excluded
coverage and the estimate before document-level consent; no omitted source
content is silently claimed to be analyzed. After a successful response, token
usage and a locally calculated actual-usage estimate are stored with the job,
without storing request or response payloads. The estimate is not the actual
billed amount and the application never reports an OpenAI balance.

### Explicit synthetic smoke test (optional; makes a billable API request)

Do this only with a test key, explicit approval, and synthetic text. From the
repository root after installing dependencies and exporting the variables above:

```sh
backend/.venv/bin/python - <<'PY'
from group_claims_backend.services.ai import OpenAIProvider

result = OpenAIProvider().interpret("Synthetic test data only: ISIN US0000000001")
print(result.model, result.total_tokens)
PY
```

This sends only the supplied synthetic string with `store=False`; it does not
test document import or authorize any stored document. Do not run it with client
content or treat its provisional result as a verified fact.
