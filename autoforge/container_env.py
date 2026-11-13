#!/usr/bin/env python3
"""Subprocess environment for container-engine clients.

Engine calls are spawned with a scrubbed environment so controller secrets never reach a
child process. Scrubbing must still carry the client variables that select *which* daemon
to talk to: a client launched with only PATH ignores DOCKER_HOST/DOCKER_CONTEXT and falls
back to /var/run/docker.sock, which fails for a sidecar (dind), remote, or rootless engine.
"""
from __future__ import annotations

import os

ALLOWED = (
    "PATH",
    "HOME",
    "TMPDIR",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
)


def engine_env() -> dict[str, str]:
    """Scrubbed environment that still addresses the configured container engine."""
    env = {key: os.environ[key] for key in ALLOWED if key in os.environ}
    env.setdefault("PATH", "")
    return env
