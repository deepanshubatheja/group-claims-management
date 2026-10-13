# Group Claims Management System

A single-user, local-first desktop application for managing group-claim holdings, claim rights, discrepancies, and traceable legal billing.

## Confirmed technology stack

- Electron for the desktop application lifecycle and secure native integration
- React, TypeScript, and Vite for the renderer user interface
- Python for the authoritative application core and domain logic
- SQLite for local structured records and evidence references

The application will support macOS and Windows. Imported source documents will be stored as immutable local copies in the operating system's per-user app-data directory, outside this repository. Any cloud AI use requires explicit authorization and is not part of this initialization phase.

## Development status

**Phase 2 — Desktop Application Initialization:** the Electron shell, secure typed preload bridge, React/TypeScript/Vite renderer, root workspace scripts, and a data-free dashboard are initialized. Python backend communication, SQLite, document handling, business logic, billing, and AI integration are intentionally not implemented.

## Local development

From the repository root:

```sh
npm install
npm run dev
```

`npm run build` produces the renderer and Electron JavaScript bundles only; it does not create a distributable installer. `npm run typecheck` checks both TypeScript workspaces.

See [docs/DEVELOPMENT_SETUP.md](docs/DEVELOPMENT_SETUP.md) for the initial development conventions and [docs/ENGINEERING_GUARDRAILS.md](docs/ENGINEERING_GUARDRAILS.md) for project-wide engineering rules.
