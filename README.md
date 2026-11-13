# AutoForgeRobot

An AI-driven code iteration toolkit that turns goals and reference code into editable, verifiable candidates.
The controller drives a model tool loop, but acceptance always comes from its own checks, real container
execution and immutable evidence — never from the model's claims.

## Core Features

- **AI tool loop** — read files, edit code, validate changes, and improve from feedback.
- **Reference learning** — reuse local source and REA analysis with traceable origins.
- **Independent validation** — verify the exact code snapshot instead of trusting the model.
- **Controlled execution** — run application checks through a Docker/Podman adapter.
- **Recoverable workflows** — pause, resume and steer candidate iterations without repeating work.

## Quick Start

Requires Python 3.11+; application execution requires a Linux Docker/Podman engine and a trusted local
Python image. Configure an OpenAI-compatible model with `AUTOFORGE_API_BASE`, `AUTOFORGE_MODEL` and
`AUTOFORGE_API_KEY` when required.

```bash
python autoforge/application_proposer.py --check-config
python autoforge/application_build.py --help
python autoforge/application_capabilities.py matrix
```

## Download

Current release: `v0.1.0-feedback-20261113` — see [latest release](https://github.com/Brian98Z/AutoForgeRobot/releases/latest)
and verify every file against the attached `SHA256SUMS.txt`. Packages are **unsigned**.

| Platform | Single file | Folder (fast start) |
| --- | --- | --- |
| Windows x64 | `autoforge-0.1.0-feedback-20261113-windows-x64.exe` (~750 ms start) | `…-windows-x64-onedir.zip` (~85 ms) |
| Linux x64 | `autoforge-0.1.0-feedback-20261113-linux-x64.tar.gz` (~310 ms) | `…-linux-x64-onedir.tar.gz` (~175 ms) |

The single-file builds unpack their payload on every start; the onedir builds are extracted once and are
roughly nine times faster to launch. GitHub's auto-generated Source code archives are not installers.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `autoforge/` | Controller modules: specification, planning, building, quality, delivery, release gates, agent tool loop, sessions, budgets and MCP client |
| `checks/` | Verification scripts; `autoforge/application_capabilities.py verify` runs the curated suite |
| `docker/` | Container iteration harness: compose topology, suite runner, baseline capture |
| `universal/` | Two-stack interpreter, Lean model, benchmark and resilience evidence |
| `src/` | Rust sandbox (WASM execution, signed envelopes, durable state) |
| `fixtures/`, `providers/`, `examples/`, `tests/` | Contracts, adapters, examples and test fixtures |
| `docs/STATUS.md` | Full status, release evidence and limitation ledger |

## Packaging

```bash
python autoforge/build_exe.py                 # onefile -> dist/autoforge.exe
python autoforge/build_exe.py --mode onedir   # onedir  -> dist-onedir/autoforge/
python autoforge/package_release.py --platform windows --version 0.1.0-feedback-20261113
```

PyInstaller cannot cross-compile; Linux packages are built inside a Python 3.11 image that also carries
`binutils`:

```bash
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim bash -lc \
  "apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils && pip install -q pyinstaller==6.22.3 && \
   python autoforge/build_exe.py --mode onefile --dist dist-linux && \
   python autoforge/build_exe.py --mode onedir  --dist dist-linux"
```

Playwright is excluded from the frozen build: browser acceptance needs the Python source installation with
Playwright and an installed browser.

## Verification

```bash
python checks/frozen_delivery_check.py dist/autoforge.exe
python checks/agent_stream_check.py
python checks/proposer_check.py
```

`frozen_delivery_check.py` exercises real spawned HTTP/SSE workers, response parsing, cancellation with zero
leftover children and `agent_mcp --help` against a deterministic loopback provider. It does not call a paid
model and does not certify generated applications.

## License

See [LICENSE](LICENSE).
