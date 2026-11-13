# AutoForge controller, Linux container form.
#
# The published release is a Windows onefile PyInstaller bundle; a Linux container cannot
# execute it. This image runs the exact same module set from source (the bundle is built
# from autoforge/*.py) with the same dispatcher entrypoint.
FROM docker:27-cli AS dockercli

# Rust controller, built from the same sources the Windows release bundles.
FROM public.ecr.aws/docker/library/python:3.11-slim AS rustbuild
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup \
    PATH=/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN set -eux; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates build-essential; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/rustup-init \
      https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup/dist/x86_64-unknown-linux-gnu/rustup-init; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --profile minimal --default-toolchain stable; \
    rm -f /tmp/rustup-init
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

# Julia runtime for the provider lanes (bootstrap/iterate/protocol parity).
FROM public.ecr.aws/docker/library/python:3.11-slim AS juliabuild
RUN set -eux; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL -o /tmp/julia.tar.gz \
      https://mirrors.tuna.tsinghua.edu.cn/julia-releases/bin/linux/x64/1.13/julia-1.13.3-linux-x86_64.tar.gz; \
    mkdir -p /opt/julia; \
    tar -xzf /tmp/julia.tar.gz -C /opt/julia; \
    rm -f /tmp/julia.tar.gz; \
    # Prune what the provider lane never reads. The precompiled stdlib cache (376 MB) is
    # omitted on purpose: Julia rebuilds only the stdlib modules a run actually loads, and
    # the image is writable, so the first run pays that cost once instead of every pull.
    rm -rf /opt/julia/julia-1.13.3/share/doc /opt/julia/julia-1.13.3/share/man \
           /opt/julia/julia-1.13.3/share/julia/test /opt/julia/julia-1.13.3/share/julia/compiled \
           /opt/julia/julia-1.13.3/share/applications /opt/julia/julia-1.13.3/share/metainfo

FROM public.ecr.aws/docker/library/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/autoforge/autoforge:/opt/autoforge

# deb.debian.org intermittently serves broken index files here; the aliyun mirror answers in ms.
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

RUN set -eux; \
    for attempt in 1 2 3; do \
        if apt-get -o Acquire::Retries=5 update \
           && apt-get install -y --no-install-recommends ca-certificates socat nodejs; then \
            break; \
        fi; \
        [ "$attempt" = 3 ] && exit 1; \
        sleep 5; \
    done; \
    rm -rf /var/lib/apt/lists/*

# Client for the sidecar Docker daemon (candidate execution happens there, never on this image).
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

RUN pip install --no-cache-dir "cryptography==43.0.3"

# Browser acceptance needs a real Chromium plus its runtime libraries. Chromium only: the
# quality check accepts an explicit engine list, so firefox/webkit stay out of the image.
# `playwright install --with-deps` fails here: it asks for ttf-unifont/ttf-ubuntu-font-family,
# which Debian 13 no longer ships. The runtime library set is installed explicitly instead.
RUN set -eux; \
    # The aliyun mirror currently fails to serve libllvm19 (pulled in by mesa), which aborts the
    # browser dependency install; the upstream archive serves it.
    sed -i 's|mirrors.aliyun.com|deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Retries=5 update; \
    apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libx11-6 libxcb1 libxext6 libxi6 libxtst6 fonts-liberation; \
    rm -rf /var/lib/apt/lists/*

# Browser binaries in their own layer: changing the browser build never re-resolves apt packages.
RUN set -eux; \
    pip install --no-cache-dir "playwright==1.49.1"; \
    # Headless shell only: the checks drive headless Chromium, and the full browser build ships a
    # second copy of the same binary plus an ffmpeg bundle nothing here uses.
    python -m playwright install --only-shell chromium; \
    rm -rf /root/.cache/ms-playwright/*.zip /root/.cache/ms-playwright/ffmpeg-* /root/.cache/pip; \
    find /usr/local/lib/python3.11/site-packages -name __pycache__ -type d -prune -exec rm -rf {} +

COPY autoforge /opt/autoforge/autoforge
COPY universal /opt/autoforge/universal
COPY checks /opt/autoforge/checks
COPY fixtures /opt/autoforge/fixtures
COPY examples /opt/autoforge/examples
COPY docker /opt/autoforge/docker
COPY src /opt/autoforge/src
COPY providers /opt/autoforge/providers
# Retained fixtures replayed verbatim by the browser-quality, framework-backend and
# stateful-operations checks; small and required for those checks to run at all.
COPY bootstrap-runs/frontend-framework-20261104 /opt/autoforge/bootstrap-runs/frontend-framework-20261104
COPY bootstrap-runs/generated-web-extended-ce8932caad84 /opt/autoforge/bootstrap-runs/generated-web-extended-ce8932caad84
COPY Cargo.toml Cargo.lock /opt/autoforge/

# Controller drivers resolve the release binary by its Windows name; a real copy there keeps
# the same checks running unchanged on Linux.
COPY --from=rustbuild /build/target/release/autoforge /opt/autoforge/target/release/autoforge.exe
COPY --from=juliabuild /opt/julia/julia-1.13.3 /opt/autoforge/runtime/julia-1.13.3
RUN set -eux; \
    cp /opt/autoforge/runtime/julia-1.13.3/bin/julia /opt/autoforge/runtime/julia-1.13.3/bin/julia.exe; \
    /opt/autoforge/runtime/julia-1.13.3/bin/julia --startup-file=no \
        --project=/opt/autoforge/providers -e 'using Pkg; Pkg.instantiate(); using JSON3'

WORKDIR /opt/autoforge
ENTRYPOINT ["/opt/autoforge/docker/entrypoint.sh"]
CMD ["python", "/opt/autoforge/autoforge/autoforge_cli.py", "--list"]
