# RaitBox concepts

A clear, from-scratch explanation of what RaitBox is and the ideas you'll meet
using it. If you just want to run something, see the [README](../README.md) and
the [example notebooks](../examples/README.md); come here to understand *why*.

---

## 1. What is an agent sandbox, and why?

An **agent sandbox** is a throwaway, isolated computer that runs code on behalf
of a program — typically an LLM agent. It matters because agent-generated code
is **untrusted**: it may be wrong, destructive, or adversarial. Running it inside
an isolated environment means a mistake (or an attack) can't touch your machine,
your data, or other users' work.

Sandboxes give you four things that plain "run this string" can't:

- **Isolation** — code runs in its own VM; the blast radius is that VM.
- **Reproducibility** — a fresh environment every time, so results are clean.
- **Real capability** — a full OS: a filesystem, shell, packages, networking.
- **Disposability** — spin up, use, throw away; run hundreds in parallel.

RaitBox provides these on AWS, behind an API compatible with
[E2B](https://e2b.dev).

---

## 2. The RaitBox model

```
your code ──▶ Sandbox (E2B-compatible API) ──▶ Backend ──▶ AWS compute
                 files · commands · run_code       │
                                                    ├─ agentcore-ci     (Code Interpreter kernel)
                                                    ├─ agentcore-runtime (managed container + shell)
                                                    └─ microvm           (Firecracker full-OS VM)
```

- You program against one small surface — `Sandbox` / `AsyncSandbox` with
  `.files`, `.commands`, `.run_code` — identical to the E2B SDK.
- A **backend** maps that surface onto a specific AWS compute service. Swapping
  backends doesn't change your code.
- Everything runs **in your own AWS account**, authenticated with your AWS
  credentials (SigV4) — there is no separate RaitBox service or API key.

**E2B-compatible** means existing E2B code runs by changing only the import
(`from e2b_code_interpreter import Sandbox` → `from raitbox import Sandbox`), and
familiar names/return types (`Execution`, `CommandResult`, `SandboxException`, …)
behave the same.

### Why go through RaitBox instead of the AWS services directly?

RaitBox sits *on top of* Lambda MicroVMs and Bedrock AgentCore; it isn't a
replacement, it's what makes them ergonomic and interchangeable:

- **Unification** — one API across four substrates. Raw, each is a different
  client, auth flow, and execution model; here they're one `Sandbox` you retarget
  with `backend=`.
- **No boilerplate** — MicroVM provisioning/polling/auth-token/health-wait/
  teardown, token refresh, the in-VM agent server, snapshot-safety hooks, and
  AgentCore session handling are done for you, not re-implemented per project.
- **Batteries the raw services lack** — background commands with streaming,
  `pause`/`resume`, preview links, secrets, volumes, LSP, PTY, computer use, warm
  pools, a declarative image builder, an MCP server, tracing, webhooks, audit, and
  network limits (see §10b).
- **Right tool per job** — cheap stateful `run_code` on AgentCore-CI, full-OS +
  inbound network on MicroVM, all without code changes; unsupported combinations
  fail loudly with `NotSupportedError` naming the alternative.
- **Your account, your rules** — standard AWS credential chain (no API key), raw
  AWS billing (no markup), and native IAM/CloudTrail/VPC/Secrets Manager/EventBridge
  integration.

---

## 3. Backends — the four substrates

The same API, four different compute types: three managed AWS services and self-hosted Dunefield. Choose with `backend=` on
`Sandbox.create()` or the `RAITBOX_BACKEND` env var.

| Backend | AWS service | Best at | Isolation | Inbound network | Notes |
|---|---|---|---|---|---|
| `agentcore-ci` *(default)* | Bedrock AgentCore Code Interpreter | `run_code` (data/analysis) | Firecracker microVM | ❌ | Stateful Jupyter kernel; scientific libs preinstalled; no internet |
| `agentcore-runtime` | Bedrock AgentCore Runtime | shell `commands`, interactive shell | per-session microVM | ❌ | Needs a deployed runtime (`agentRuntimeArn`); managed minimal container |
| `microvm` | AWS Lambda MicroVMs | full OS, installing packages, servers | Firecracker microVM | ✅ (`get_host`) | Internet egress; `git`/`pip`; needs a built image ARN |
| `dunefield` | Dunefield cell (EKS, self-hosted) | many sandboxes in your VPC, warm pools, models without credentials | gVisor (user-space kernel) | ✅ (`get_host`, via the Router) | Needs a Dunefield cell and a registered template; egress only if the template allows it |

**How to pick:** start on `agentcore-ci` (zero setup beyond AWS creds). Use
`microvm` when you need internet, to install packages, run `git`, or expose a
port. Use `agentcore-runtime` when you have a deployed agent runtime and want its
persistent shell. Use `dunefield` when you operate a Dunefield cell: sandboxes run in
your VPC, start from warm pools, and reach models through the cell without credentials.

**Capability, not magic:** if a backend can't do something (e.g. `get_host` on
AgentCore, or `git apply` where there's no `git`), RaitBox raises
`NotSupportedError` naming the alternative — mismatches fail loudly, never silently.

---

## 4. Executing code — two paths

- **`run_code(code, language=...)`** runs in the backend's **language kernel**.
  On `agentcore-ci` this is a **stateful IPython kernel**: variables persist
  across calls (like notebook cells), and the last expression's value is captured
  as `.text`. Returns an `Execution` with `.logs.stdout/.stderr`, `.results`, and
  `.error`. Crucially, **errors are returned, not raised** — an
  `ExecutionError(name, value, traceback)` — so an agent can read the failure and
  try again.

- **`commands.run(cmd, ...)`** runs an **arbitrary shell command**, returning a
  `CommandResult(stdout, stderr, exit_code)`. A **non-zero exit raises**
  `CommandExitException` (matching E2B). Pass `on_stdout`/`on_stderr` to **stream**
  output live. `background=True` returns a `CommandHandle` you can iterate,
  `wait()` on, `kill()`, or feed stdin — for servers, builds, long test runs.

Rule of thumb: `run_code` for "evaluate this Python/JS in a kernel"; `commands`
for "run this program in a shell".

---

## 5. Files and artifacts

`sandbox.files` reads/writes the sandbox filesystem — `write`, `read`
(`format="bytes"` for binary), `list`, `exists`, `remove`, `rename`, `make_dir`,
`watch_dir`. The filesystem is **ephemeral**: it's gone when the sandbox is
killed (unless you snapshot it — §7).

The **artifact pattern** is how you get non-text outputs out of a sandbox: have
code write a file (a chart PNG, a report, a build artifact), then pull it back
with `files.read(path, format="bytes")`. Notebook `04` uses exactly this to
retrieve a matplotlib chart.

---

## 6. Async and concurrency

`AsyncSandbox` is the awaitable twin of `Sandbox`. Because each sandbox is an
isolated session, you can run **many at once** with `asyncio.gather` — the basis
for parallel evaluation (`10`), pass@k sampling (`07`), and vectorized RL rollouts
(`15`). RaitBox offloads the (synchronous) AWS calls to threads, so concurrency
works without an async AWS SDK.

---

## 7. Sessions, lifecycle, and cost

- A sandbox is a **session** that **bills while it runs**. Always use
  `with Sandbox() as sbx:` (or `AsyncSandbox`) so it's killed on exit.
- **Guardrails:** RaitBox tracks live sessions and an `atexit` hook stops any you
  forgot to `kill()` (emitting a `ResourceWarning`); `RAITBOX_MAX_SESSIONS=N`
  caps concurrency.
- **`set_timeout`** extends a session's idle lifetime; **`connect(sandbox_id)`**
  reattaches to an existing one; **`Sandbox.list()`** enumerates running sandboxes
  (where the backend supports it).
- **pause / resume** — `pause()` snapshots the workspace **files** to S3 and stops
  the sandbox; `resume()` restores them into a fresh one. *Files survive; live
  in-memory/kernel state does not.* A pragmatic "durable sandbox".

---

## 8. Networking

Only `microvm` has an **inbound** network path: each VM gets a per-VM HTTPS
endpoint, so a server you start inside it is reachable via `get_host(port)`.
Outbound internet (for `pip`/`git`) is controlled by **egress network connectors**
(e.g. `INTERNET_EGRESS`). AgentCore backends have no ingress — retrieve results
with `files.read()` instead.

---

## 9. Authentication

RaitBox uses the **standard AWS credential chain** (env vars, profile, or role) and
signs requests with **SigV4** — there is no RaitBox API key. The `api_key`
argument exists only for E2B signature compatibility and is ignored.

**LLMs** in the examples run on **Amazon Bedrock** (`bedrock-runtime`), also via
SigV4 — no third-party model key. Two Bedrock paths appear:

- **Converse API** (`bedrock-runtime`) — model-agnostic; used with boto3 directly
  or `langchain-aws`. Auth = AWS SigV4. This is the default in the examples.
- **Responses API** — the OpenAI-compatible endpoint on `bedrock-runtime`; used
  with the OpenAI SDK / `langchain-openai`. Auth = a **Bedrock API key** (a
  separate long-term key), not SigV4. Only notebook `11` uses this.

---

## 10. Agent & evaluation concepts (what the examples teach)

- **Tool use** — you expose `execute_python` as a tool; the model decides when to
  call it, you run the code in a sandbox and feed the result back. The sandbox is
  the tool's execution backend (`03`).
- **Feedback loop** — don't trust generated code; *run* it, and on failure return
  the error so the model fixes it. The simplest self-correcting agent (`02`).
- **Executable reward** — score a solution by *running* its tests (fraction
  passing), not by inspection. Used across the eval/RL notebooks.
- **Rule-based (similarity) reward** — score by textual similarity to a reference
  solution (SWE-RL). Cheap, no execution, but blind to behavior. `09` compares the
  two.
- **pass@k** — sample *k* solutions; solved if *any* passes. Measures how much
  sampling helps (`07`).
- **Isolated per-task evaluation** — run each candidate in its own sandbox so
  untrusted code is safe and results don't leak between tasks (`05`, `10`).
- **SWE-bench-style** — the model's artifact is a **unified diff** applied with
  `git apply` to a real repo; reward = the repo's test suite going FAIL→PASS
  (`14`).
- **SWE-Gym-style RL environment** — wrap the above as a Gym `reset()`/`step()`
  loop with an executable reward, and log `(observation, action, reward, done)`
  **trajectories** for training data (`15`).
- **Verifiable-task synthesis** — generate new tasks and use the sandbox to keep
  only ones that are *verifiable* (reference passes; a trivial stub fails) (`08`).

---

## 10b. Extensions beyond E2B (additive)

These add capability without changing the E2B surface — existing E2B code is
unaffected, and each raises `NotSupportedError` on backends that can't provide it.

- **Preview links** — `sbx.get_preview_link(port)` returns a `PreviewLink`
  (`url`, `token`, `port`, `.headers`) so a caller/browser can reach an HTTP
  server running inside the VM. Backed by `create_microvm_auth_token` scoped to
  the port and expiring in `expires_in_minutes` (default 30). `microvm` only.
- **Volumes** — persistent, shareable storage backed by an **existing** EFS file
  system (bring-your-own resource; RaitBox mounts, it does not provision).
  `Sandbox.create_volume(name, file_system_id=...)` / `Sandbox.list_volumes()`
  register/enumerate; `volume_mounts=[VolumeMount(fs_id, "/data", subpath=...)]`
  NFS-mounts them at VM start via the `/run` hook. `microvm` only.
- **Secrets** — `secrets={ENV: ref}` where `ref` is `secretsmanager:<id>[::<json-key>]`,
  `ssm:<name>`, or a literal. Resolved at start via the AWS credential chain and
  merged into `envs` (explicit `envs` win on clash; nothing is logged). All
  backends (the values flow through the normal env path).
- **LSP** — `sbx.lsp.create_server(language, project_root)` returns an
  `LspServer` backed by a real language-server process in the VM (`pylsp` for
  Python, `typescript-language-server` for JS/TS). `did_open(path)` then
  `document_symbols(path)` / `workspace_symbols(query)` / `completions(path,
  Position(line, char))`. The agent server speaks LSP JSON-RPC over stdio and
  exposes semantic ops over HTTP. `microvm` only.
- **PTY** — `sbx.pty.create(PtySize(rows, cols), on_data=...)` attaches a real
  pseudo-terminal (openpty + a shell) and returns a `PtyHandle`;
  `send_stdin(bytes)` / `resize(...)` / `kill()` / `wait()`. `microvm` only.
- **Human access** — `sbx.get_terminal_link(port)` launches a web-terminal
  server (`ttyd`) in the background and returns a `PreviewLink` a person can open
  in a browser. Any human-facing server (a noVNC desktop, a Jupyter UI) works the
  same way: start it on a port, then `get_preview_link(port)`. `microvm` only.
- **Computer use** — `sbx.computer_use` drives a virtual desktop for GUI agents:
  `computer_use.start()` brings up Xvfb, then `mouse.move/click/scroll`,
  `keyboard.type/press/hotkey`, `screenshot.take()` (PNG bytes), and
  `display.get_info()`. Backed by `xdotool`/`scrot` against the display; needs a
  desktop stack in the image. `microvm` only.
- **Warm pools** — `WarmPool(size=N, backend=..., **create_kwargs)` pre-starts
  sandboxes in background threads; `acquire()` returns one instantly when warm
  (falls back to an inline create when empty) and replenishes the pool. Cuts
  cold-start latency. Client-side (no server state); works on any backend.
- **Declarative image builder** — `Image().dnf_install(...).pip_install(...)
  .run_commands(...).env(...).workdir(...)` describes a MicroVM image in Python;
  the steps inject into the agent-server Dockerfile, and
  `build_image(..., image=img)` builds it. The **snapshot registry** —
  `tooling.build_image.list_images()` / `delete_image(arn)` — enumerates and
  cleans up built images.
- **MCP server** — `python -m raitbox.mcp` (needs `raitbox[mcp]`) exposes sandbox
  operations (create/run_code/run_command/files/kill) as MCP tools so clients
  like Claude Desktop can drive sandboxes. `RaitboxTools` holds the logic;
  `build_server()` wires it to `FastMCP`.
- **Observability** — off by default and dependency-free. Call
  `observability.configure(service_name=...)` (or set `RAITBOX_TRACING=1` with
  `opentelemetry-api` installed) and create/run_code/run_command emit OTel spans
  tagged with backend + sandbox id — no code changes to existing calls.
- **Webhooks** — off by default. `events.configure(bus_name=..., source=...)` (or
  `RAITBOX_EVENT_BUS`) makes sandbox `created`/`killed` emit Amazon EventBridge
  events; an EventBridge rule with an API-destination target forwards them to any
  HTTPS webhook. Delivery is best-effort and never breaks a sandbox operation.
- **Audit logs** — every sandbox is provisioned through AWS control-plane APIs, so
  AWS records them in CloudTrail automatically. `audit.lookup_events(source=
  "microvm", hours=24)` reads that trail back as `{name, time, username,
  request_parameters}` records — a tamper-evident audit log with no SDK-side
  logging.
- **Network limits** — MicroVM egress is **deny-by-default**: a VM reaches the
  network only through the VPC `egress_connectors` you attach. `internet_access=
  False` (microvm) hard-locks egress; `internet_access=True` requires an explicit
  connector, so accidental open networking fails loudly.

---

## 11. Where things live

```
src/raitbox/
  client/      Sandbox / AsyncSandbox facades (the E2B-compatible surface)
  core/        types, errors, settings, session pool, handles, persistence
  backends/    agentcore_ci · agentcore_runtime · microvm (+ HTTP transport)
  agent_server/  FastAPI app baked into the MicroVM image
  tooling/     build a MicroVM image
```

- Public API + feature matrix: [README](../README.md)
- Backend setup, IAM, image build, live tests: [INTEGRATION.md](INTEGRATION.md)
- Hands-on, concept-by-concept: [examples/](../examples/README.md)
