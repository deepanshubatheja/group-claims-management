# Group Claims Management System

A single-user, local-first desktop application for managing group-claim holdings, claim rights, discrepancies, and traceable legal billing.

## Confirmed technology stack

- Electron for the desktop application lifecycle and secure native integration
- React, TypeScript, and Vite for the renderer user interface
- Python for the authoritative application core and domain logic
- SQLite for local structured records and evidence references

The application will support macOS and Windows. Imported source documents will be stored as immutable local copies in the operating system's per-user app-data directory, outside this repository. Any cloud AI use requires explicit authorization and is not part of this initialization phase.

## Development status

**Phase 3 — Local Backend Foundation:** Electron now supervises a development-only Python child process using a private newline-delimited JSON protocol. The secure typed preload bridge exposes a health check, and the dashboard displays actual backend runtime information. SQLite, document handling, business logic, billing, AI integration, and production backend packaging remain intentionally unimplemented.

## Local development

From the repository root:

```sh
npm install
npm run dev
```

`npm run build` produces the renderer and Electron JavaScript bundles only; it does not create a distributable installer. `npm run typecheck` checks both TypeScript workspaces.

Create the local Python environment before starting Electron development:

```sh
python3 -m venv backend/.venv
```

Run Electron process-boundary tests with `npm run test:electron`. Backend protocol tests are documented in [backend/README.md](backend/README.md).

See [docs/DEVELOPMENT_SETUP.md](docs/DEVELOPMENT_SETUP.md) for the initial development conventions and [docs/ENGINEERING_GUARDRAILS.md](docs/ENGINEERING_GUARDRAILS.md) for project-wide engineering rules.
