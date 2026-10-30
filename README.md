# AutoForgeRobot

An AI-driven code iteration toolkit that turns goals and reference code into editable, verifiable candidates.

## Core Features

- **AI tool loop** — read files, edit code, validate changes, and improve from feedback.
- **Reference learning** — reuse local source code and REA analysis with traceable origins.
- **Independent validation** — verify the exact code snapshot instead of trusting the model's claims.
- **Controlled execution** — run application checks through a Docker/Podman adapter.
- **Recoverable workflows** — pause and resume candidate iterations without blindly repeating interrupted operations.

## Quick Start

Requires Python 3.11+. Configure an OpenAI-compatible model using `AUTOFORGE_API_BASE`, `AUTOFORGE_MODEL`, and `AUTOFORGE_API_KEY` when required.

```bash
python application_proposer.py --check-config
python application_build.py --help
```

Application execution requires a Linux Docker/Podman engine and a trusted local Python image.

## Status

Active development. Real-model tool interaction and syntax repair have been tested. End-to-end container business execution remains unverified; the tool-driven agent does not yet support streaming or tool-level resume.
