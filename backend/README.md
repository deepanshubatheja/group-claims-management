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
