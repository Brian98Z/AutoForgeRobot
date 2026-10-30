# AutoForgeRobot

A controlled, AI-driven code iteration prototype.

AutoForge connects language models to tools for reading references, editing code, validating candidates, and improving them from feedback. Model output alone never counts as a successful result.

## Core Features

- **AI tool loop:** native tool calls for reading, version-checked editing, and validation.
- **Reference-driven development:** import local source code and REA evidence with content hashes and provenance.
- **Independent validation:** accept candidates only through a verifier report bound to the exact code snapshot.
- **Controlled execution:** a Docker/Podman adapter for isolated Python application checks; no unrestricted host shell for the agent.
- **Recovery:** pause and resume the whole-candidate workflow; interrupted external calls are not blindly replayed.
- **Dual-track experiments:** Python and Julia propose integer WASM programs, while Rust verifies and records signed local releases.

## Current Status

The minimal tool loop has been tested with a real local language model, including feedback-driven syntax repair. This verifies tool interaction and syntax checking—not business correctness.

Container-based business execution is implemented but has not yet been validated end to end. The tool-driven agent does not yet support streaming or tool-level resume. This is not a production deployment system or a general-purpose autonomous application builder.

## Getting Started

Python 3.11+ is required for the application tools. Configure an OpenAI-compatible model with `AUTOFORGE_API_BASE`, `AUTOFORGE_MODEL`, and, when required, `AUTOFORGE_API_KEY`.

```powershell
python application_proposer.py --check-config
python agent_runtime_check.py
python application_build.py --help
```

Application runs require an available Linux Docker/Podman engine and a trusted, locally installed Python image pinned by its exact image ID. Preflight blocks model invocation when required execution prerequisites are missing.

The Rust/WASM track additionally requires Rust and Julia. Runtime binaries, models, private state, and local verification artifacts are not included in the source distribution.

See [README.md](README.md) for detailed Chinese documentation.
