# Python backend development

The backend is a private local child process. It communicates only through newline-delimited UTF-8 JSON over standard input/output; it does not bind an HTTP port.

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

Each request and response is one UTF-8 JSON object per line. The protocol version is `1.0`, requests are limited to 65,536 bytes, and the only allowed command in this phase is `health.check`.

```json
{"protocolVersion":"1.0","id":"health-1","command":"health.check","params":{}}
```

Protocol messages are written exclusively to stdout. Operational logs use stderr and never include raw request content.

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
