# Developer setup

ClaimFlow is an Electron + React/TypeScript + Vite + Python + SQLite desktop application. This guide is for developers running the source code; normal users should install the DMG instead.

## Requirements

macOS Apple Silicon is the supported development and packaging environment.

- Git
- Node.js 22+ and npm 10+
- Python 3.11+
- Xcode Command Line Tools (`xcrun swiftc`)
- Tesseract OCR with English (`eng`) and German (`deu`) language data

Check the installation:

```zsh
node --version
npm --version
python3 --version
xcode-select -p
tesseract --list-langs
```

Tesseract is required for OCR and for the macOS package build. Install it and its language data with your normal package manager before continuing.

## First run

From the repository root:

```zsh
git clone <repository-url>
cd group-claims-management
npm ci
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install -e backend
npm run build:credential-helper:mac
npm run dev
```

Electron should open automatically. Stop the development app with `Ctrl-C` in the terminal that runs it.

`build:credential-helper:mac` builds the local Keychain helper used by **Settings → AI Connection**. Run it again after changing the helper or after a clean checkout. The app can still run local-only features if the helper is unavailable, but saving an API key will show a controlled error.

## AI configuration

AI is optional. In the running app:

1. Open **Settings → AI Connection**.
2. Paste an OpenAI API key and select **Save API Key**.
3. Select **Check Connection**.
4. Enable Cloud AI before deliberately requesting analysis or Q&A.

The key is stored in the signed-in macOS user's Keychain, not in the repository, SQLite, or a configuration file. Never commit API keys, `.env` files, or screenshots containing keys.

The connection check sends no document content. AI requests send only bounded, relevant extracted evidence after the user explicitly requests analysis or asks a question; original vault files are kept local. Use non-sensitive sample data for development and testing unless you have appropriate authorization.

## Run checks

Run these from the repository root:

```zsh
npm run typecheck
npm run test:frontend
npm run test:electron
```

Run backend tests separately:

```zsh
cd backend
./.venv/bin/python -m unittest discover -s tests -v
```

## Build a macOS testing installer

With the requirements above installed:

```zsh
npm run package:mac
```

The Apple Silicon DMG is written to `release/`. The build includes Electron, the production renderer, a PyInstaller-packaged Python backend, and the local Tesseract runtime/language data. Client machines do not need Python, Node.js, npm, SQLite, or Tesseract installed.

The current macOS build is unsigned and not notarized. It is for local or trusted internal testing with non-sensitive sample data only. Do not describe Windows as supported until its installer is built and tested on Windows.

## Data and source-control rules

- Runtime databases, immutable imported-document copies, exports, backups, logs, and settings live in the per-user application-data directory, outside the repository and install directory.
- Do not delete, overwrite, or move runtime data to troubleshoot a source checkout unless the user explicitly approves it.
- Do not commit `node_modules/`, `backend/.venv/`, `release/`, generated installers, API keys, or client documents.
- Keep renderer code inside the typed, allowlisted Electron IPC boundary. Do not add Node.js access or secrets to the renderer.

## Common setup failures

- **Backend unavailable:** recreate/activate `backend/.venv`, reinstall with `backend/.venv/bin/python -m pip install -e backend`, then restart `npm run dev`.
- **Cannot save API key:** run `npm run build:credential-helper:mac`; verify Xcode Command Line Tools are selected with `xcode-select -p`.
- **OCR unavailable:** run `tesseract --list-langs` and ensure `eng` (and, where needed, `deu`) is listed.
- **Packaging fails:** confirm the Node, Python, Xcode, and Tesseract requirements above, then rerun `npm run package:mac`.
