# RaitBox

![python](https://img.shields.io/badge/python-3.10%2B-blue)
![lint](https://img.shields.io/badge/ruff-passing-brightgreen)
![types](https://img.shields.io/badge/mypy-clean-brightgreen)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

RaitBox gives LLM agents **disposable, isolated computers on AWS** — a
filesystem, a shell, and a code kernel an agent can use without ever touching
your machine or your data. One Python API targets **three AWS compute engines**
(Bedrock AgentCore Code Interpreter, AgentCore Runtime, and Lambda MicroVMs) plus
self-hosted [Dunefield](#dunefield-self-hosted-on-eks) cells (gVisor on EKS), and
that API matches the [E2B](https://e2b.dev) SDK — so agent code written for E2B
runs on RaitBox by changing a single import. Everything executes **inside your own
AWS account**, under your IAM, region, and billing; there is no RaitBox service
and no third-party key.

> **The name** — "Rait" comes from the Hindi word **रेत** (*rait*), "sand." A
> RaitBox is, fittingly, a sand-box.

## Quickstart

```bash
pip install -e .           # from a clone (not yet on PyPI)
export AWS_PROFILE=... AWS_DEFAULT_REGION=...
```

```python
from raitbox import Sandbox

with Sandbox() as sbx:                                     # defaults to agentcore-ci
    print(sbx.run_code("print(6 * 7)").logs.stdout)        # ['42\n']
    sbx.files.write("app.py", "print('hi from a file')")
    print(sbx.commands.run("python3 app.py").stdout)       # hi from a file
```

New to sandboxes, backends, or the eval/RL patterns here? Start with
[docs/CONCEPTS.md](docs/CONCEPTS.md) — it builds the mental model from scratch.

## Four compute engines, one API

```
your code ─▶ Sandbox / AsyncSandbox ─▶ backend ─▶ AWS compute
              files · commands · run_code   │
                                            ├─ agentcore-ci      Bedrock AgentCore Code Interpreter
                                            ├─ agentcore-runtime Bedrock AgentCore Runtime
                                            ├─ microvm           AWS Lambda MicroVMs (full-OS VM)
                                            └─ dunefield         Dunefield cell (gVisor on EKS, self-hosted)
```

You program against one small surface; a **backend** maps it onto a specific AWS
compute type. Switch with `backend=` on `Sandbox.create()` or `RAITBOX_BACKEND` —
no code change. When a backend can't do something, RaitBox raises
`NotSupportedError` naming the alternative, never a silent no-op.

| Backend | AWS service | Best for | `template=` | Inbound net |
|---|---|---|---|---|
| `agentcore-ci` *(default)* | AgentCore Code Interpreter | stateful `run_code`, files; zero setup | Code Interpreter id (default `aws.codeinterpreter.v1`) | ❌ |
| `agentcore-runtime` | AgentCore Runtime | shell `commands`, interactive shell | an existing `agentRuntimeArn` | ❌ |
| `microvm` | AWS Lambda MicroVMs | full OS, internet, packages, servers | a built MicroVM image ARN | ✅ (`get_host`) |
| `dunefield` | Dunefield cell (EKS + gVisor), self-hosted | high-density sandboxes in your VPC, warm pools, model access without credentials | a registered template `name:vN` | ✅ (`get_host`, via the Router) |

**Rule of thumb:** start on `agentcore-ci` (nothing to provision). Move to
`microvm` when you need a real OS — internet egress, `pip`/`git`, background
servers, or an inbound port. Use `agentcore-runtime` when you already run a
deployed agent runtime and want its persistent shell. Use `dunefield` when you run
a Dunefield cell and want many sandboxes inside your own VPC (see below).

### Dunefield (self-hosted on EKS)

Dunefield runs RaitBox's agent server in gVisor
sandboxes on EKS behind a private Sandbox Router. The backend authenticates with your AWS
credentials (a presigned STS token bound to the cell; no API keys) and needs:

| Setting (kwarg / env var) | Meaning |
|---|---|
| `gateway_url` / `RAITBOX_DUNEFIELD_GATEWAY_URL` | Router URL: the cell's private Gateway, or a loopback tunnel to it |
| `audience` / `RAITBOX_DUNEFIELD_AUDIENCE` | cell host name; defaults to the gateway host, required for tunnels |
| `tls_server_name` / `RAITBOX_DUNEFIELD_TLS_SERVER_NAME` | pin the TLS name (and `Host`) when tunnelling |
| `ca_bundle` / `RAITBOX_DUNEFIELD_CA_BUNDLE` | CA bundle for a privately issued Gateway certificate |
| `template` / `RAITBOX_DUNEFIELD_TEMPLATE` | default template (`name:vN`, built by Dunefield's template pipeline) |

```python
sbx = Sandbox.create(backend="dunefield", template="python-agent:v3",
                     gateway_url="https://use1-dev.sandboxes.dunefield.internal")
```

Templates are built from `agent_server/Dockerfile.dunefield` and registered by Dunefield's
pipeline; network egress and model access are template labels, so sandboxes have no egress
unless the template allows it. `envs=` skips the warm pool (a cold start of a few seconds).

## What you get beyond a raw sandbox

RaitBox is a layer *over* AWS's compute — it isn't a competitor to Lambda
MicroVMs or AgentCore, it's what makes them ergonomic and interchangeable:

- **No AWS plumbing.** MicroVM lifecycle (launch → poll to `RUNNING` → mint a
  port-scoped token → health-wait → terminate), token refresh, the in-VM agent
  server, snapshot-safety hooks, and AgentCore session handling are all handled.
- **Batteries the services don't ship.** Background commands with live streaming,
  `pause`/`resume` file snapshots, preview links, secret injection, EFS volumes,
  language servers, an interactive PTY, computer use, warm pools, a declarative
  image builder + registry, an MCP server, OpenTelemetry tracing, EventBridge
  webhooks, CloudTrail audit, and deny-by-default network limits.
- **Your account, your governance.** No API key (standard AWS credential chain),
  no vendor markup (raw AWS billing), and native IAM / CloudTrail / VPC egress /
  Secrets Manager / SSM integration.
- **Typed and verified.** Ships `py.typed`; unit-tested with fakes and exercised
  end-to-end against a live AWS account.

These extensions are **additive** — they never change the E2B-compatible surface.
Full list and semantics in [docs/CONCEPTS.md](docs/CONCEPTS.md); per-backend
support in the [capability matrix](#capability-matrix) below.

## Drop-in for E2B

Existing E2B code moves onto your AWS footprint by changing only the import:

```diff
- from e2b_code_interpreter import Sandbox, AsyncSandbox
+ from raitbox import Sandbox, AsyncSandbox
```

The names, keyword arguments, return dataclasses (`Execution`, `Logs`,
`CommandResult`, `EntryInfo`, …), and exception hierarchy (`SandboxException`,
`CommandExitException`, `TimeoutException`, `GitUpstreamException`, …) match E2B,
covering:

- `Sandbox()` / `.create()` / `.connect()` / context managers; instance **and**
  static `kill` / `set_timeout` / `is_running` / `get_info` / `get_metrics`; and
  `Sandbox.list()` → `SandboxPaginator`
- `run_code(...)` with `language`/`context`/`envs` and the streaming callbacks,
  plus `create_code_context` / `list_code_contexts` / `restart_code_context` /
  `remove_code_context`, and rich `Result` (`.formats()`, `_repr_*_`)
- `commands.run(..., background=True)` → `CommandHandle` (iterate / `wait` /
  `kill` / `send_stdin`) and `commands.list/kill/send_stdin/connect`
- `files.read/write/write_files/list/exists/get_info/remove/rename/make_dir/watch_dir`
- `sandbox.git`, `pause()`/`resume()`, `open_shell()`, and the full `AsyncSandbox`

**Deliberately unsupported** (present, but raise `NotSupportedError` so calls fail
loudly rather than `AttributeError`): server-side `fork` / `create_snapshot` /
`list_snapshots` (use `pause`/`resume`), `update_network`, `get_mcp_token`, and
the unified `E2B(...)` client.

## Capability matrix

| Capability | `agentcore-ci` | `agentcore-runtime` | `microvm` | `dunefield` |
|---|---|---|---|---|
| `run_code` | ✅ stateful IPython kernel | ✅ one-shot | ✅ stateful namespace | ✅ stateful namespace |
| `create_code_context` (per-context state, one sandbox) | ✅ fresh kernel (`clearContext`) | 🟡 stateless per call | 🟡 shared namespace | 🟡 shared namespace |
| `commands.run` (foreground) | ✅ | ✅ streamed | ✅ | ✅ |
| `commands.run(background=True)` → `CommandHandle` | ❌ | ❌ | ✅ real processes | ✅ real processes |
| `commands.list/kill/send_stdin/connect` | ❌ | ❌ | ✅ | ✅ |
| `files.*` (read/write/list/exists/get_info/remove/rename/make_dir) | ✅ (`rename`/`make_dir` via shell) | ✅ over shell | ✅ | ✅ |
| `files.watch_dir` → `WatchHandle` | ✅ polling | ✅ polling | ✅ polling | ✅ polling |
| `pause()` / `resume()` (S3 file snapshot) | ✅ | ✅ | ✅ | ✅ (from the client) |
| interactive shell (`open_shell`, async) | ❌ | ✅ WebSocket | ❌ | ❌ |
| `pty.create/send_stdin/resize/kill` (sync) | ❌ | ❌ | ✅ real TTY | ✅ real TTY |
| `get_host(port)` | ❌ | ❌ | ✅ | ✅ Router port route (IAM token) |
| `Sandbox.list()` | ✅ sessions | ❌ | ✅ | ✅ |
| `AsyncSandbox` | ✅ | ✅ | ✅ | ✅ |
| `get_preview_link(port)` *(extension)* | ❌ | ❌ | ✅ signed proxy URL | ✅ caller's IAM token |
| `create_volume` / `volume_mounts=` *(extension)* | ❌ | ❌ | ✅ EFS | ❌ |
| `secrets={...}` injection *(extension)* | ✅ | ✅ | ✅ | ✅ |
| `lsp.create_server(...)` *(extension)* | ❌ | ❌ | ✅ pylsp / tsserver | ✅ pylsp (in the template) |
| `get_terminal_link(...)` web terminal *(extension)* | ❌ | ❌ | ✅ ttyd + preview | 🟡 needs `ttyd` in the template |
| `computer_use.*` desktop automation *(extension)* | ❌ | ❌ | ✅ Xvfb + xdotool | ❌ no desktop template yet |

**Reading it:** ✅ supported · 🟡 supported with a caveat · ❌ not available. An
❌ is either a **substrate limit** (the service can't do it — e.g. AgentCore has
no inbound network path, so `get_host`/preview/terminal stay ❌; the Code
Interpreter kernel has no TTY/processes/display, so `pty`/`lsp`/`computer_use` stay
❌ there) or a **not-yet-wired** capability the service *does* expose (EFS volumes
on either AgentCore backend; background commands + `Sandbox.list()` on
`agentcore-runtime`). Details in [docs/INTEGRATION.md](docs/INTEGRATION.md).

Behavioral highlights:

- **`run_code`** — `agentcore-ci` persists kernel variables and echoes the last
  expression; `run_code(envs={...})` is honored on all backends (on the stateful
  `agentcore-ci`/`microvm` kernels the vars persist to later runs).
- **`create_code_context`** isolates *state between contexts within one sandbox* —
  **not** VM isolation (every backend is sandbox-isolated). For a hard boundary
  between workloads, use **separate sandboxes**.
- **`commands.run`** raises `CommandExitException` on non-zero exit; on
  `agentcore-ci` output is pty-merged into `.stdout`.
- **`pause()`/`resume()`** copy files to S3 and restore them into a fresh sandbox:
  **files survive, in-memory state does not** (needs `bucket=` or
  `RAITBOX_SNAPSHOT_BUCKET`).
- **`secrets={ENV: "secretsmanager:id::key" | "ssm:name" | literal}`** resolves via
  the credential chain and merges into `envs` (explicit `envs` win; nothing is
  logged). On `agentcore-ci` secrets reach both `run_code` and `commands.run`.
- **Extensions** are `microvm`-only unless noted (see the matrix for `dunefield`): `get_preview_link` mints a
  short-lived, port-scoped token + `X-aws-proxy-*` headers; `volume_mounts=` NFS-
  mounts an existing EFS filesystem (bring-your-own — RaitBox never provisions
  storage); `pty`, `lsp`, and `computer_use` drive real in-VM processes.

```python
# Background command with live streaming (microvm)
handle = sbx.commands.run("npm test", background=True)
for chunk in handle:
    print(chunk, end="")
result = handle.wait()                    # -> CommandResult

# Interactive shell (agentcore-runtime, async)
async with await sbx.open_shell() as shell:
    await shell.send("ls -la\n")
    async for frame in shell:
        print(frame.text, end="")
```

## Setup

**Install** — from a clone (not yet on PyPI); extras pull in only what a given
backend/feature needs:

```bash
pip install -e .               # core: agentcore-ci + agentcore-runtime
pip install -e '.[runtime]'    # + WebSocket shell for agentcore-runtime
pip install -e '.[server]'     # + deps to build the MicroVM agent-server image
pip install -e '.[mcp]'        # + MCP server (python -m raitbox.mcp)
pip install -e '.[dev]'        # + pytest, ruff, mypy, boto3-stubs
```

**Auth & region** — the standard AWS credential chain (no E2B key; `api_key=` is
accepted for signature parity and ignored):

```bash
export AWS_PROFILE=raitbox-dev
export AWS_DEFAULT_REGION=us-west-2
```

**Cost** — sessions bill until stopped. RaitBox tracks every sandbox and an
`atexit` hook reclaims any you forget to `kill()`; cap concurrency with
`RAITBOX_MAX_SESSIONS=N`. Prefer `with Sandbox() as sbx:`.

**Building a MicroVM image** — the `microvm` backend talks to a RaitBox **agent
server** baked into your image (`src/raitbox/agent_server/`):

```bash
python -m raitbox.tooling.build_image \
    --name raitbox-sandbox \
    --code-bucket my-build-bucket \
    --build-role-arn arn:aws:iam::<acct>:role/raitbox-build-role \
    --region us-east-1
# add --egress-connector <ARN> (repeatable) if build steps fetch from the internet;
# prints the image ARN to pass as template=
```

Minimal caller IAM: `lambda:{RunMicrovm,GetMicrovm,ListMicrovms,
CreateMicrovmAuthToken,TerminateMicrovm}` + `iam:PassRole` on the VM execution
role (builds also need `lambda:CreateMicrovmImage` and S3). The `lambda-microvms`
client needs boto3 ≥ 1.43. Full IAM, roles, and per-backend env vars:
[docs/INTEGRATION.md](docs/INTEGRATION.md).

> **Status:** pre-release (0.1.0). API shapes are validated against the live
> botocore service models and unit-tested with fakes; all backends and extensions
> have also been exercised end-to-end against a live AWS account. APIs may change
> before 1.0.

## Examples

`examples/` is a set of Jupyter notebooks ordered by increasing complexity;
LLM-backed ones use **Amazon Bedrock** (`bedrock-runtime`).

| # | Notebook | Shows |
|---|---|---|
| 01 | `01-basics` | the sandbox API: run_code, files, commands, errors, async, backends |
| 02 | `02-agent-feedback-loop` | Claude (Bedrock Converse) writes code, sandbox runs it, errors fed back to retry |
| 03 | `03-claude-code-interpreter` | Claude with a code-execution tool (tool-use loop) |
| 04 | `04-data-analysis-chart` | Claude analyzes data, saves a chart, notebook downloads & renders it |
| 05 | `05-eval-harness` | model solutions scored in isolated per-task sandboxes |
| 06 | `06-iterative-self-repair` | write a module, run pytest, fix failures until green |
| 07 | `07-pass-at-k` | pass@1 / pass@k across concurrent isolated sandboxes |
| 08 | `08-verifiable-task-synthesis` | LLM invents tasks; sandbox validates reference + discriminating tests |
| 09 | `09-reward-comparison` | similarity reward vs sandbox execution reward, side by side |
| 10 | `10-parallel-eval-runner` | dataset × agent across concurrent isolated sandboxes → report |
| 11 | `11-langgraph-bedrock-code-interpreter` | LangGraph agent via Bedrock Responses API (needs a Bedrock API key) |
| 12 | `12-background-and-snapshots` | background `CommandHandle`, `get_host`, pause/resume (microvm) |
| 13 | `13-claude-code-cli-in-sandbox` | Claude Code CLI in a MicroVM, model via Bedrock |
| 14 | `14-swebench-lite` | SWE-bench-style: model diff → `git apply` → re-run suite (microvm) |
| 15 | `15-swe-gym-env` | SWE-Gym-style `reset`/`step` env, executable reward → trajectory JSONL (microvm) |
| 16 | `16-full-stack-preview` | serve an agent-built web app via a signed preview link; `secrets` + volume (microvm) |
| 17 | `17-lsp-guided-agent` | ground a coding agent with real `sbx.lsp` symbols before it edits (microvm) |
| 18 | `18-computer-use-agent` | screenshot → model → mouse/keyboard loop with `sbx.computer_use` (microvm + desktop image) |
| 19 | `19-warm-pool-eval` | cut cold starts with `WarmPool`; read the win from OpenTelemetry spans |
| 20 | `20-interactive-pdb-pty` | hold a live `pdb` session over `sbx.pty` (microvm) |
| 21 | `21-custom-image-and-mcp` | declarative `Image` builder + snapshot registry, then expose sandboxes over MCP |

Notebooks **16–21** showcase the extensions beyond the E2B surface; see
[examples/README.md](examples/README.md) for the guided path and prerequisites.

## Testing

```bash
make check                 # ruff (lint + format), mypy, pytest — the full gate
pytest                     # unit tests only, no AWS needed
pytest -m integration      # opt-in live tests (bills short sessions)
```

Live tests skip unless AWS credentials work and per-backend env vars are set —
see [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Project layout

```
src/raitbox/
  client/        E2B-compatible facades — sync.py (Sandbox), aio.py (AsyncSandbox)
  core/          types · errors · settings · session pool · handles · persistence ·
                 secrets · warm_pool · observability · events · audit
  backends/      agentcore_ci · agentcore_runtime · microvm · dunefield, the shared
                 agent-server client (_agent_server) and HTTP transports
  agent_server/  FastAPI app baked into the MicroVM image and Dunefield templates
  tooling/       build_image.py · image.py (declarative image builder)
  mcp/           MCP server exposing sandboxes as tools
tests/           unit tests (no AWS) + tests/integration (opt-in, live)
examples/        21 tutorial notebooks
docs/            CONCEPTS · INTEGRATION · DESIGN · aws-e2b-api-notes
```

## Documentation & contributing

- **Concepts & mental model:** [docs/CONCEPTS.md](docs/CONCEPTS.md)
- **Backend setup, IAM, image build, live tests:** [docs/INTEGRATION.md](docs/INTEGRATION.md)
- **Guided notebooks:** [examples/README.md](examples/README.md)
- **Design notes / API references:** [docs/DESIGN.md](docs/DESIGN.md) ·
  [docs/aws-e2b-api-notes.md](docs/aws-e2b-api-notes.md)
- **Contributing & releases:** [CONTRIBUTING.md](CONTRIBUTING.md) ·
  [CHANGELOG.md](CHANGELOG.md)

## License

[Apache-2.0](LICENSE). See also [NOTICE](NOTICE).
