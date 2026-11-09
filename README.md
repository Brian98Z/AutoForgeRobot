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
python autoforge/application_proposer.py --check-config
python autoforge/application_build.py --help
python autoforge/application_capabilities.py matrix
```

Configure an OpenAI-compatible model with `AUTOFORGE_API_BASE`, `AUTOFORGE_MODEL`, and `AUTOFORGE_API_KEY` when required.

## Download

Windows x64 feedback update: [latest release](https://github.com/Brian98Z/AutoForgeRobot/releases/latest), `v0.1.0-feedback-20261109`. Download `autoforge-0.1.0-feedback-20261109-windows-x64.exe` and verify it against `SHA256SUMS.txt`. This updated executable is **unsigned**; the old self-signed certificate does not sign it. Historical `v0.1.0` remains available separately. Source archives attached by GitHub are not installers.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `autoforge/` | Controller modules: specification, planning, building, quality, delivery, release gates, the native agent tool loop, sessions, budgets and the MCP client |
| `checks/` | Verification scripts; `autoforge/application_capabilities.py verify` runs the curated suite |
| `universal/` | Two-stack interpreter, Lean model, benchmark and resilience evidence |
| `src/` | Rust sandbox (WASM execution, signed envelopes, durable state) |
| `fixtures/`, `providers/`, `examples/`, `tests/` | Contracts, adapters, examples and test fixtures |
| `docs/STATUS.md` | Full status, release evidence and limitation ledger |

## Single-file controller executable

Build a Windows onefile executable with Python 3.11 and PyInstaller 6.22.3:

```bash
python autoforge/build_exe.py
```

The build writes `dist/autoforge.exe`; the dispatcher accepts a bundled module CLI:

```powershell
.\dist\autoforge.exe --list
.\dist\autoforge.exe agent_mcp --help
.\dist\autoforge.exe application_capabilities matrix
.\dist\autoforge.exe application_release check --candidate ... --spec ...
```

Windows multiprocessing workers are handled before module dispatch. Frozen subprocess calls using `sys.executable script.py` or `sys.executable -c` remain supported. Playwright is excluded from the standalone EXE: browser acceptance requires the Python source installation, Playwright and an installed browser. The curated suite is not claimed to have native parity inside the frozen binary.

```bash
python checks/frozen_delivery_check.py dist/autoforge.exe
```

This exercises real spawned loopback HTTP/SSE, complete assistant-turn parsing, metadata compatibility, cancellation/worker cleanup and the MCP CLI; it does not call a paid model.

## MCP integration

AutoForge includes a bounded stdio MCP **client** (`agent_mcp.py`) for controller-side discovery and calls (`list`, `call`). See [docs/MCP.md](docs/MCP.md) for configuration and safety bounds. It is not an AutoForge MCP server, does not inject external MCP tools into the model loop, and does not replace model API configuration.

## November 9 fixes and verification

- Corrected the PyInstaller entrypoint, frozen multiprocessing dispatch and stale verification-script paths after the repository layout change.
- Accept SSE `service_tier` metadata and nullable usage without weakening malformed-stream refusal.
- Publish public staged candidate source with explicit non-root read/traverse permissions; secret storage permissions are unchanged.
- Reject failed required browser resources such as `/app.js` HTTP 401; preserve restart-phase diagnostics.
- Keep Android offline SDK regression fixtures isolated from network fetches.
- Complete post-fix curated rerun: **41 PASS / 7 BLOCKED / 0 FAIL**. The seven declared default/opt-in boundaries remain BLOCKED, not silently promoted.
- Frozen Web candidate: existing independent oracle rerun **33/33 PASS**, administrator/reader workflows passed in Chromium/Firefox/WebKit, restart persistence passed, and the existing local package validated. These are scoped results, not universal correctness or production-security certification.

Image-safety release and external semantic approval remain blocked. No safety baseline was switched and no production application was deployed. The bounded deterministic-maintenance call-count benefit does not establish general learning, cross-domain or monetary savings.

## Verification

```bash
python autoforge/application_capabilities.py verify --timeout 120 --output verification.json
```

Each check reports `PASS`, `BLOCKED`, or `FAIL`. `BLOCKED` marks a declared boundary (unconfigured engine, database, browser, or opt-in model), not a silent pass. Detailed status and evidence binding live in [docs/STATUS.md](docs/STATUS.md). Local runtime evidence, private state, keys, caches and executable artifacts are not part of the source tree.
