# AutoForgeRobot

An AI-driven code iteration toolkit that turns goals and reference code into editable, verifiable candidates.

## Core Features

- **AI tool loop** — read files, edit code, validate changes, and improve from feedback.
- **Reference learning** — reuse local source code and REA analysis with traceable origins.
- **Independent validation** — verify the exact code snapshot instead of trusting the model's claims.
- **Controlled execution** — run application checks through a Docker/Podman adapter.
- **Recoverable workflows** — pause and resume candidate iterations without blindly repeating interrupted operations.

## Quick Start

Requires Python 3.11+ and, for application execution, a Linux Docker/Podman engine with a trusted local Python image.

```bash
python application_proposer.py --check-config
python application_build.py --help
python application_capabilities.py matrix
```

Configure an OpenAI-compatible model with `AUTOFORGE_API_BASE`, `AUTOFORGE_MODEL`, and `AUTOFORGE_API_KEY` when required.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `application_*.py` | Controller: specification, planning, building, quality, delivery, release gates |
| `agent_*.py` | Native tool loop, sessions, streaming, recovery, budget and stop controls |
| `*_check.py` | Verification scripts; `application_capabilities.py verify` runs the curated suite |
| `src/` | Rust sandbox (WASM execution, signed envelopes, durable state) |
| `fixtures/`, `providers/`, `examples/`, `tests/` | Contracts, adapters, examples and test fixtures |
| `docs/STATUS.md` | Full status, release evidence and limitation ledger |

## Single-file controller executable

Build a Windows onefile executable with Python 3.11 and PyInstaller 6.22.3:

```bash
python build_exe.py
```

The build writes `dist/autoforge.exe` and a dispatcher entry (`autoforge_cli.py`) accepts any bundled module CLI:

```powershell
.\dist\autoforge.exe --list
.\dist\autoforge.exe application_capabilities matrix
.\dist\autoforge.exe application_release check --candidate ... --spec ...
```

Frozen subprocess calls using `sys.executable script.py` or `sys.executable -c` are routed back through the dispatcher. The curated 48-script suite is not claimed to have native parity inside the frozen binary.

## Verification

```bash
python application_capabilities.py verify --timeout 120 --output verification.json
```

Each check reports `PASS`, `BLOCKED`, or `FAIL`. `BLOCKED` marks a declared boundary (unconfigured engine, database, browser, or opt-in model), not a silent pass. Detailed status and evidence binding live in `docs/STATUS.md`.
