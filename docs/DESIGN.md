# RaitBox — Implementation Plan

**RaitBox** is a Python agent-sandbox library with an **E2B-compatible API**, backed by
AWS compute: **Lambda MicroVMs** and **Bedrock AgentCore** (Runtime + Code Interpreter).

Write code against the familiar E2B `Sandbox` API; sandboxes run in *your own AWS
account* with Firecracker microVM isolation, SigV4 auth, and AWS billing.

```python
# from e2b_code_interpreter import Sandbox    # E2B
from raitbox import Sandbox                    # RaitBox

with Sandbox() as sbx:
    print(sbx.run_code("print(6*7)").logs.stdout)   # ['42\n']
    sbx.files.write("app.py", "print('hi')")
    print(sbx.commands.run("python3 app.py").stdout)
```

## 1. Goals & non-goals

**Goals**
- E2B-compatible public surface: `Sandbox`, `AsyncSandbox`, `sbx.files`, `sbx.commands`,
  `sbx.pty`, `run_code`, matching E2B method names, signatures, and result dataclasses so
  existing E2B code works by changing only the import.
- Pluggable **backends** selected at runtime; ship three:
  1. `agentcore-code-interpreter` — stateful IPython kernel, files, commands (best `run_code`).
  2. `agentcore-runtime` — one-shot streamed commands + persistent WebSocket shell (best `commands`).
  3. `lambda-microvm` — full-OS Firecracker VM reached over its HTTPS endpoint via an in-VM agent server.
- Sync + async, unit-testable without AWS (mocked clients), plus opt-in live integration tests.
- Cost guardrails: atexit reclamation of leaked sessions, max-concurrency cap, context managers.

**Non-goals (v0.1)**
- The E2B cloud control plane (volumes, templates registry, snapshots marketplace).
- Rich Jupyter display results beyond text for the Code Interpreter backend (service returns text).
- Full parity for every E2B beta method (`fork`, `update_network`, MCP token) — stub with `NotSupportedError`.

## 2. Package layout

```
raitbox/
  __init__.py            # re-exports Sandbox, AsyncSandbox, models, exceptions
  _config.py             # backend selection, env vars, region/profile resolution
  exceptions.py          # E2B-compatible exception hierarchy
  models.py              # Execution/Logs/Result/ExecutionError/CommandResult/EntryInfo/...
  registry.py            # live-session tracking + atexit cleanup + max-concurrency
  sandbox.py             # E2B-compatible sync facade -> delegates to a Backend
  async_sandbox.py       # async facade (asyncio.to_thread over sync, + native async shell)
  backends/
    __init__.py          # BACKENDS registry, get_backend(name)
    base.py              # Backend ABC / Protocol: lifecycle, exec, files, net
    agentcore_ci.py      # AgentCore Code Interpreter backend (port of reference)
    agentcore_runtime.py # AgentCore Runtime backend (invoke_agent_runtime_command + shell)
    _agent_server.py     # AgentServerBackend: code/commands/files/pty/lsp over the agent server
    microvm.py           # Lambda MicroVMs backend (boto3 + HTTPS endpoint client)
    dunefield.py         # Dunefield backend (Sandbox Router control API + agent server)
    _router_http.py      # Dunefield-IAM signed HTTP client to the Sandbox Router
    _ci_invoker.py       # code-interpreter event-stream normalization
    _microvm_http.py     # signed HTTP client to the microVM endpoint + token mgmt
  snapshot.py            # S3 file-snapshot pause/resume (shared helper)
  agent_server/          # ships INSIDE the microVM image (the in-VM E2B-op server)
    __init__.py
    server.py            # HTTP app: /run_code /run_command /files/* + lifecycle hooks
    Dockerfile           # FROM public.ecr.aws/lambda/microvms:al2023-minimal
    requirements.txt
  tooling/
    build_image.py       # create_microvm_image + poll build state
tests/
  conftest.py            # fake boto3 clients
  test_models.py
  test_agentcore_ci.py
  test_agentcore_runtime.py
  test_microvm.py
  test_async.py
  integration/           # -m integration, hits real AWS
examples/
  01-quickstart.ipynb ... 10-claude-code-cli-in-sandbox.ipynb
  (numbered notebooks by increasing complexity; LLM ones use Bedrock)
pyproject.toml  README.md  LICENSE
```

## 3. Backend abstraction (`backends/base.py`)

```python
class Backend(ABC):
    # lifecycle
    def create(self, *, template, timeout, envs, metadata, region, profile, **kw) -> "Backend"
    def kill(self) -> bool
    def set_timeout(self, timeout: int) -> None
    def is_running(self) -> bool
    def get_info(self) -> SandboxInfo
    @property
    def sandbox_id(self) -> str
    # execution
    def run_code(self, code, language="python", **cb) -> Execution
    def run_command(self, cmd, *, envs, cwd, user, on_stdout, on_stderr,
                    background, timeout) -> CommandResult | CommandHandle
    def create_pty(self, size, **kw) -> CommandHandle          # optional -> NotSupportedError
    # filesystem
    def file_read(self, path, format="text") -> str | bytearray
    def file_write(self, path, data) -> WriteInfo
    def file_list(self, path, depth=1) -> list[EntryInfo]
    def file_remove/rename/make_dir/exists/get_info(...)
    # networking
    def get_host(self, port: int) -> str                        # NotSupportedError where N/A
```

`Sandbox` holds a `Backend` and forwards; `files`/`commands`/`pty` are thin namespace
adapters over the backend. This isolates AWS specifics and lets one facade serve all three.

## 4. Backend mappings

### 4a. `agentcore-code-interpreter`  (client `bedrock-agentcore`)
Own implementation; API shapes verified against the live AWS service models
(botocore) — not copied from any external code.
- create -> `start_code_interpreter_session(codeInterpreterIdentifier=template or "aws.codeinterpreter.v1", sessionTimeoutSeconds=timeout)`
- `run_code` -> `invoke_code_interpreter(name="executeCode", arguments={code, language, clearContext})`, stateful IPython kernel; restore trailing `\n`.
- `commands.run` -> `executeCommand`; **pty quirk**: merged stdout+stderr arrive in `structuredContent.stderr`; expose as `.stdout`; raise `CommandExitException` on non-zero.
- files -> `writeFiles/readFiles/listFiles/removeFiles` (native dict `arguments`, text/blob).
- kill -> `stop_code_interpreter_session`. `get_host` -> `NotSupportedError`.
- pause/resume -> S3 file snapshot (`snapshot.py`); files survive, kernel memory does not.

### 4b. `agentcore-runtime`  (control `bedrock-agentcore-control`, data `bedrock-agentcore`)
- create -> caller supplies an existing `agentRuntimeArn` via `template=` (creating a runtime
  needs an ECR image / zip deploy — out of scope to auto-provision; document + optional helper).
  Generate a `runtimeSessionId` (UUID, **≥33 chars**) to pin the session/microVM.
- `commands.run` -> `invoke_agent_runtime_command(body={command, timeout})`; consume the
  event stream (`contentStart`/`contentDelta{stdout,stderr}`/`contentStop{exitCode,status}`),
  fire `on_stdout`/`on_stderr`, build `CommandResult`. Stateless per command (chain with `&&`).
- interactive pty -> `bedrock_agentcore.runtime.AgentCoreRuntimeClient.open_shell` (WebSocket,
  async) exposed through `AsyncSandbox.pty`; sync `pty` -> `NotSupportedError` (WS is async).
- `run_code` -> wrap as `invoke_agent_runtime_command('python3 -c ...')` (non-stateful) OR
  route to the CI backend; default: run via command, no rich results.
- files -> implemented via shell (`cat`/`base64`/redirect) helpers since Runtime has no file API.
- kill -> `stop_runtime_session(agentRuntimeArn, runtimeSessionId, qualifier)`.

### 4c. `lambda-microvm`  (client `lambda-microvms`, IAM prefix `lambda:`)
MicroVMs expose **no exec API** — you talk HTTP to the app baked into the image. RaitBox ships
that app (`agent_server/`) and a client that speaks to it.
- create -> `run_microvm(imageIdentifier=template, idlePolicy={autoResumeEnabled, maxIdleDurationSeconds, suspendedDurationSeconds}, maximumDurationInSeconds=timeout)`;
  read `microvmId` + `endpoint`; poll `get_microvm` until `RUNNING`.
- auth -> `create_microvm_auth_token(microvmIdentifier, expirationInMinutes, allowedPorts=[{allPorts:{}}])`
  -> `authToken["X-aws-proxy-auth"]`; refresh before expiry.
- exec/files -> HTTPS to `https://{endpoint}` with header `X-aws-proxy-auth` (`_microvm_http.py`):
  `POST /run_code`, `POST /run_command` (chunked stream), `POST /files/{read,write,list,...}`.
  The `agent_server` implements these + required lifecycle hooks
  (`/aws/lambda-microvms/runtime/v1/{ready,run,resume,suspend,terminate}`) and generates
  per-VM entropy in `/run` (snapshot-safety requirement).
- suspend/resume -> `suspend_microvm`/`resume_microvm`; kill -> `terminate_microvm`.
- `get_host(port)` -> returns the endpoint host (supported here). Shell -> `SHELL_INGRESS` +
  `create_microvm_shell_auth_token` (later).
- Image build tooling (`tooling/build_image.py`): zip Dockerfile+agent_server -> S3 ->
  `create_microvm_image(baseImageArn="arn:aws:lambda:<region>:aws:microvm-image:al2023-1", buildRoleArn=...)`,
  poll version state `PENDING->IN_PROGRESS->SUCCESSFUL`.

### 4d. `dunefield`  (a self-hosted Dunefield cell: gVisor on EKS)
Same agent server as 4c, reached through the cell's Sandbox Router instead of a per-VM endpoint.
Both backends inherit the data plane (`/run_code`, `/run_command`, `/files/*`, `/commands/*`,
`/pty/*`, `/lsp/*`) from `AgentServerBackend`; each supplies its own transport (`AgentHttp`).
- auth -> `Authorization: Dunefield-IAM v1.<token>`: a presigned STS `GetCallerIdentity` URL
  with a signed `x-dunefield-audience` header naming the cell (15 minutes, refreshed).
- create -> `POST /v1/sandboxes {template, timeout, envs, metadata, resources}`, then poll the
  agent's `/health`. get/list/timeout/kill -> `GET`/`PATCH`/`DELETE /v1/sandboxes[/{id}]`.
- data calls -> the agent-server routes on the Router with header `X-Sandbox-ID`.
- `get_host(port)`/`preview_link` -> `/v1/sandboxes/{id}/ports/{port}` on the Router, authorized
  with the caller's own token. `computer_use` -> `NotSupportedError` (no desktop template).

## 5. Config & backend selection (`_config.py`)
- `backend=` kwarg on `Sandbox.create()`, else env `RAITBOX_BACKEND`
  (`agentcore-ci` [default] | `agentcore-runtime` | `microvm`).
- AWS creds via standard chain; `region_name`/`profile_name` kwargs. `api_key` accepted+ignored
  (E2B signature compat). Snapshot bucket via `RAITBOX_SNAPSHOT_BUCKET`.
- Concurrency cap via `RAITBOX_MAX_SESSIONS`.

## 6. E2B compatibility surface (must match)
- `Sandbox.create(template, timeout, metadata, envs, secure, allow_internet_access, ...)`,
  `Sandbox.connect(id)`, static/instance `kill`, `set_timeout`, `get_info`, `list`, `is_running`,
  context manager. Deprecated bare `Sandbox()` -> `create()`.
- `commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, timeout)` overloaded
  fg->`CommandResult`, bg->`CommandHandle`; `list/kill/send_stdin/connect`.
- `files.read(format="text"|"bytes"|"stream")`, `write`, `write_files`, `list(depth)`, `exists`,
  `get_info`, `remove`, `rename`, `make_dir`, `watch_dir`.
- `pty.create(size)/send_stdin/resize/kill`.
- `run_code(code, language|context, on_stdout,on_stderr,on_result,on_error, envs, timeout)` ->
  `Execution(results, logs, error, execution_count, .text)`; `create_code_context` etc.
- Models: `Execution, Logs, Result, ExecutionError, CommandResult, CommandHandle, EntryInfo,
  WriteInfo, SandboxInfo, PtySize`.
- Exceptions: `SandboxException` base; `TimeoutException, InvalidArgumentException,
  NotFoundException(+File/Sandbox), RateLimitException, AuthenticationException,
  CommandExitException, NotSupportedError`.
- Unsupported-per-backend features raise `NotSupportedError` with a clear message.

## 7. Async (`async_sandbox.py`)
- `AsyncSandbox` mirrors sync via `asyncio.to_thread` (no aioboto3 dep), preserving
  `async with AsyncSandbox.create() as sbx:`; dual instance/static forms for `kill/connect/...`.
- Native async only where required: AgentCore Runtime `open_shell` (WebSocket) for `pty`.

## 8. Testing
- Unit: fake boto3 clients returning recorded shapes (verified against the live
  service models) — no AWS. Cover each backend's mapping, quirks (pty merge, trailing newline,
  non-zero exit), files round-trip, async wrappers, registry cleanup/cap.
- `agent_server`: FastAPI `TestClient` unit tests.
- Integration (`-m integration`, off by default): short live sessions per backend.
- `pyproject.toml` markers: `integration`; `addopts = "-m 'not integration'"`.

## 9. Build phases
1. Scaffold: pyproject, `__init__`, exceptions, models, config, registry, backend ABC, facades.
2. `agentcore-ci` backend + unit tests -> first green, usable.
3. `agentcore-runtime` backend (command stream + async shell) + tests.
4. `lambda-microvm` backend: HTTP client + `agent_server` + `tooling/build_image.py` + tests.
5. Examples, README, docs, packaging polish; full test pass.

## 10. Dependencies
- Runtime: `boto3>=1.43`, `requests` (microVM HTTP), `bedrock-agentcore` (optional extra for
  Runtime shell), `websockets` (via bedrock-agentcore). `agent_server` extra: `fastapi`,`uvicorn`.
- Dev: `pytest>=8`. Python `>=3.10`.

## Key sources
- AWS service models (botocore) for `lambda-microvms`, `bedrock-agentcore`,
  `bedrock-agentcore-control`, and `bedrock-runtime` — the source of truth for
  every API shape, verified live.
- The public E2B Python SDK surface — the compatibility target.
