#!/bin/sh
# Container entrypoint: optional model relay, optional engine wait, then exec the command.
set -eu

# The controller refuses plain HTTP for non-loopback endpoints (application_proposer._endpoint).
# The host model listens on 127.0.0.1:8081, which a container cannot reach directly; relay the
# host endpoint onto this container's own loopback so AUTOFORGE_API_BASE stays loopback.
if [ -n "${AUTOFORGE_MODEL_FORWARD:-}" ]; then
    socat "TCP-LISTEN:${AUTOFORGE_MODEL_PORT:-8081},fork,reuseaddr,bind=127.0.0.1" \
          "TCP:${AUTOFORGE_MODEL_FORWARD}" &
fi

# Temporary staging directories become bind-mount sources for candidate containers, so they
# must live on the volume the sidecar engine also mounts; the container's own /tmp is not
# visible to that daemon.
if [ -n "${TMPDIR:-}" ]; then
    mkdir -p "$TMPDIR"
fi

# The same rule applies to the controller tree itself: any check that mounts controller source
# into a candidate container needs a path the sidecar daemon can resolve, so the workbench runs
# from a copy on the shared volume. The Julia runtime and the Rust binary stay in the image and
# are linked in: they are executed locally, never mounted, and copying ~1 GB per start is waste.
if [ -n "${AUTOFORGE_WORKSPACE_COPY:-}" ]; then
    rm -rf "$AUTOFORGE_WORKSPACE_COPY"
    mkdir -p "$AUTOFORGE_WORKSPACE_COPY"
    tar -C /opt/autoforge --exclude=./runtime --exclude=./target -cf - . \
        | tar -C "$AUTOFORGE_WORKSPACE_COPY" -xf -
    ln -s /opt/autoforge/runtime "$AUTOFORGE_WORKSPACE_COPY/runtime"
    ln -s /opt/autoforge/target "$AUTOFORGE_WORKSPACE_COPY/target"
    : > "$AUTOFORGE_WORKSPACE_COPY/.ready"
fi

if [ -n "${DOCKER_HOST:-}" ] && [ "${DOCKER_WAIT:-1}" = "1" ]; then
    i=0
    until docker info >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 120 ]; then
            echo "engine at ${DOCKER_HOST} unreachable" >&2
            exit 1
        fi
        sleep 1
    done
fi

exec "$@"
