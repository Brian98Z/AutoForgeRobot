# Containerized AutoForge workbench

Runs the AutoForge controller on Linux instead of the frozen Windows executable, with the
model-driven iteration loop pointed at a local OpenAI-compatible model server.

## What the image contains

- Python 3.11 controller modules (`/opt/autoforge/autoforge/*.py`)
- Rust controller binary (`/opt/autoforge/target/release/autoforge.exe`, ELF)
- Julia runtime (`/opt/autoforge/runtime/julia-1.13.3`)

The container seeds `/workspace/autoforge` from `/opt/autoforge` on start (tar copy excluding
`runtime/` and `target/`, which are then symlinked back into the image) and writes
`.ready` when the copy is complete. `docker/suite.py` waits for that marker.

## Build and run

```bash
docker build -f docker/Dockerfile -t autoforge:0.1.0-linux .
cd docker && docker compose up -d
docker compose exec -T autoforge python autoforge/autoforge_cli.py --list
```

`docker compose` also starts the `docker:27-dind` sidecar. Controller checks reach it through
`DOCKER_HOST=tcp://docker:2375`, so any path a check mounts into a candidate container must
exist on the shared `/workspace` volume — that is why the controller tree is executed from the
seeded copy rather than from `/opt`.

## Model wiring

The controller only accepts plain HTTP to a loopback endpoint, so the container forwards its own
loopback to the host model:

| env | value | meaning |
|---|---|---|
| `AUTOFORGE_API_BASE` | `http://127.0.0.1:8081/v1` | endpoint the controller sees |
| `AUTOFORGE_MODEL_FORWARD` | `host.docker.internal:8081` | real host endpoint (`socat` on start) |
| `AUTOFORGE_MODEL` | served model id | must equal an id listed by `/v1/models`; mismatch fails closed |

Current host model (Qwen3.6-35B-A3B, IQ4_NL, 19.8 GB) does not fit the 16 GB VRAM card alone, so
it is started with llama.cpp automatic layer fitting:

```powershell
D:\LLAMA\llama-server.exe `
  -m "<models-dir>\Qwen3.6-35B-A3B\model.gguf" `
  --mmproj "<models-dir>\Qwen3.6-35B-A3B\mmproj.gguf" `
  --host 127.0.0.1 --port 8081 --fit on --fit-ctx 16384 -c 32768 --parallel 1 `
  --flash-attn on -ub 512 -b 1024 --threads 16 --threads-batch 16 `
  -ctk q8_0 -ctv q8_0 --load-mode mmap --no-reasoning-preserve
```

Switching to the smaller Qwen3.5-9B service on host port 8084 only requires changing
`AUTOFORGE_MODEL_FORWARD` and `AUTOFORGE_MODEL`; the controller refuses a model id that the
endpoint does not list.

Start it with no console at all: a server that owns a console receives `^C` when the launching
job is reaped, and `cmd /c start` still gives it one. Creating the process through WMI directly is
the only variant observed to survive; `powershell -File start-detached.ps1` holds the exact argv.
Context size matters more than layer count here: at `-c 32768` the 19.8 GB model leaves under
1 GB of VRAM free and drops to roughly a fifth of the throughput, so the runbook uses `-c 16384`
with `--fit on` and 12 threads:

```powershell
Set-Content "$env:TEMP\qwen36-8081.cmd" @'
@echo off
cd /d D:\LLAMA
llama-server.exe -m ... -m --host 127.0.0.1 --port 8081 --fit on ... 1> out.log 2> err.log
'@
powershell -NoProfile -Command "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c start \"qwen36-8081\" /min \"%TEMP%\\qwen36-8081.cmd\"'}"
```

`start` gives the server its own console; a plain WMI/`Start-Process` launch still shares the
caller's console and is interrupted on teardown.

## Self-iteration cycle

```bash
docker compose exec -T autoforge python autoforge/application_build.py run \
  --request /workspace/demo/request.json --contract /workspace/demo/contract.json \
  --reference-store /workspace/demo/store \
  --engine docker --image sha256:<exact local python image id> \
  --output /workspace/runs/<run-id> \
  --allow-model --trusted-adapter --mode agent --action-profile small \
  --max-turns 40 --token-budget 400000 --time-budget 1200
```

`--image` must be a full `sha256:` id present in the sidecar daemon; short ids and pulls are
refused. Acceptance is decided by the independent builder, not by the model's own messages.

## What the image carries (2.41 GB)

- Playwright 1.49.1 with the Chromium **headless shell** only: the checks drive headless Chromium,
  and the full build ships a second copy of the same binary plus an unused ffmpeg bundle.
  Runner libraries are installed explicitly because `--with-deps` asks Debian 13 for fonts it no
  longer ships. That layer resolves packages against `deb.debian.org` because the aliyun mirror
  currently fails to serve `libllvm19` (pulled in by mesa) and aborts the install.
- Julia 1.13.3 without `share/doc`, `share/man`, the stdlib test tree and the precompiled stdlib
  cache. Dropping the cache saves 376 MB; Julia rebuilds only the stdlib modules a run loads, so
  the first run pays that once inside the writable image. `protocol_check` still finishes in ~62 s.
- Docker CLI, node, and the two retained fixtures below.
- Two retained fixtures (5 MB) that the browser-quality, framework-backend and stateful-operations
  checks replay verbatim: `bootstrap-runs/frontend-framework-20261104` and
  `bootstrap-runs/generated-web-extended-ce8932caad84`.
- Nothing else from `bootstrap-runs/`; the rest of the evidence tree stays out of the build context.

Candidate images for the toolchain/framework checks live in the sidecar daemon, not in this image:
`node:20-slim` and `eclipse-temurin:21-jdk` are resolved to their exact ids at run time.

## Verification suite

```bash
docker compose exec -T autoforge python docker/suite.py --container-profile --timeout 480
docker compose exec -T autoforge python docker/baseline.py \
  --report /workspace/runs/suite-<stamp>.json --engine-image <image> --label <label>
```

`--container-profile` supplies the arguments each live check needs (engine image, fresh paths,
node image resolved from the sidecar daemon at run time) and bounds `endurance_check` to 12
rounds / 4 segments. It also supplies per-check environment inputs (the recovery check takes the
run-time engine image id through `AUTOFORGE_RECOVERY_IMAGE`) and raises the stateful
reconstruction budget to 24 model calls / 1M tokens, because the driver default of 12 stops this
model mid-iteration. A profiled check whose required image is missing is recorded as
`skipped: no local image …` instead of failing on a placeholder. Baseline records are
append-only at `/workspace/autoforge/bootstrap-runs/docker-baseline/iterations.jsonl`.

Checks and the suite are baked into the image: copying a patched file into a running container is
discarded by `docker compose up --force-recreate`. Rebuild the image, or patch it offline with
`docker create` + `docker cp` + `docker commit` when a rebuild would need network.

## Remaining non-PASS checks (72/75 on the latest baseline record)

| check | reason |
|---|---|
| `application_provenance_check` | needs the detached GPG signature and pinned publisher fingerprint of the Julia artifact, which this image does not carry |
| `frozen_delivery_check` | exercises a Windows executable's spawn/SSE path; the Linux image ships the Rust controller instead |
| `benchmark_independent_check` | accepts only the previously authorized benchmark image; widening it would remove the guard |

None of these are silent passes; each returns an explicit HOLD/BLOCKED with its own reason.
