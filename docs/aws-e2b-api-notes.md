# API research notes (verified from AWS docs + E2B SDK reference)

Source of truth for backend implementation. All API shapes were verified live
against the AWS service models (botocore). Below: the three backends and the E2B
surface.

## Lambda MicroVMs  (`boto3.client("lambda-microvms")`, IAM prefix `lambda:`)

No exec API — reach the app you bake into the image over the per-VM HTTPS endpoint.

Runtime ops: `run_microvm`, `get_microvm`, `list_microvms`, `suspend_microvm`,
`resume_microvm`, `terminate_microvm`, `create_microvm_auth_token`,
`create_microvm_shell_auth_token`.
Image ops: `create_microvm_image`, `update_microvm_image` (base-image-arn + build-role-arn
required every build call), `get/list/delete_microvm_image`, `update_microvm_image_version`
(state ACTIVE|INACTIVE), `list_managed_microvm_images`.

`run_microvm(imageIdentifier=<arn>, imageVersion?, executionRoleArn?, maximumDurationInSeconds=1..28800, idlePolicy={autoResumeEnabled:bool, maxIdleDurationSeconds<=28800, suspendedDurationSeconds}, runHookPayload<=16KB, logging?, ingressNetworkConnectors?, egressNetworkConnectors?)`
-> `{microvmId, state, endpoint, ...}`. States: PENDING->RUNNING->SUSPENDING->SUSPENDED->TERMINATING->TERMINATED.

Endpoint: `https://<microvm-id>.lambda-microvm.<region>.on.aws`. Auth token:
`create_microvm_auth_token(microvmIdentifier, expirationInMinutes, allowedPorts=[{allPorts:{}}|{port:N}|{range:{startPort,endPort}}])`
-> `{authToken:{"X-aws-proxy-auth":"<JWE>"}}`. Send requests with header `X-aws-proxy-auth: <token>`;
port select via header `X-aws-proxy-port: N` (default 8080; must be in token allowedPorts else 403).
Reserved `X-aws-proxy-*` stripped before forwarding. Protocols: HTTP/1.1, h2, WebSocket, gRPC, SSE.

Ingress connectors (ARNs): `ALL_INGRESS`, `NO_INGRESS`, `SHELL_INGRESS`
(`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:ALL_INGRESS`).
Egress: `INTERNET_EGRESS` or customer VPC connector (via `lambda-core create-network-connector`).
Connectors can't change while a VM runs; must be ACTIVE.

Image build: zip(Dockerfile + artifacts) -> S3 -> `create_microvm_image(name, codeArtifact={uri:s3://...},
baseImageArn="arn:aws:lambda:<region>:aws:microvm-image:al2023-1", buildRoleArn, environmentVariables<=50,
readyTimeoutInSeconds 1..3600)`. Build runs Dockerfile, starts app, waits `/ready` HTTP 200, snapshots
disk+memory+processes. Container base: `public.ecr.aws/lambda/microvms:al2023-minimal` (Linux, ARM/x86).

Lifecycle hooks (POST `/aws/lambda-microvms/runtime/v1/<name>`): `ready`, `validate` (build role);
`run`, `resume`, `suspend`, `terminate` (execution role). `/run` body:
`{"microvmId":"...","runHookPayload":"..."}`. **Snapshot safety**: all VMs from one image version
share identical state — generate unique IDs/entropy in `/run` (use `secrets`/`/dev/urandom`), never
at build. Re-establish custom network connections in `/resume`.

Build role: assume by lambda.amazonaws.com; `s3:GetObject`, logs:*, ecr:* (private).
Execution role: runtime AWS access + logs. Log group `/aws/lambda-microvms/<image-name>`.

Errors: ThrottlingException (backoff+jitter), InternalServerException, ServiceQuotaExceededException,
ResourceNotFoundException, ValidationException. Endpoint HTTP: 400/403/429/500/502.

## AgentCore Runtime  (control `bedrock-agentcore-control`, data `bedrock-agentcore`)

Session isolation = per-session microVM (up to 8h) or Instances on managed EC2 (up to 14d).
`runtimeSessionId`: caller-supplied, **>=33 chars** (UUID); reuse to pin session + keep state.
No list-sessions op — track ids yourself.

Control: `create_agent_runtime(agentRuntimeName, agentRuntimeArtifact={containerConfiguration:{containerUri}},
networkConfiguration={networkMode:PUBLIC}, roleArn, lifecycleConfiguration={idleRuntimeSessionTimeout, maxLifetime})`
-> `{agentRuntimeArn, status}`. Container: ARM64, port 8080, `POST /invocations` + `GET /ping`.
ZIP code-deploy alt (250MB, 25 sessions/s) via AgentCore CLI `CodeZip`.

Data — one-shot command (HTTP/2 stream):
```
invoke_agent_runtime_command(agentRuntimeArn, runtimeSessionId(>=33), qualifier="DEFAULT",
  contentType="application/json", accept="application/vnd.amazon.eventstream",
  body={command:"/bin/bash -c \"...\"", timeout:1..3600})
for event in resp["stream"]:
  chunk = event["chunk"]
  contentStart | contentDelta{stdout,stderr} | contentStop{exitCode,status:COMPLETED|TIMED_OUT}
```
Stateless per command (chain with `&&`). Non-zero exit is NOT an API error. 25 TPS.
`RetryableConflictException` (409) transient. command 1B-64KB. Needs runtime built after 2026-03-17.

Persistent shell (WebSocket, async) via Python SDK `pip install bedrock-agentcore`:
```
from bedrock_agentcore.runtime import AgentCoreRuntimeClient, ShellChannel
client = AgentCoreRuntimeClient(region=...)
async with client.open_shell(runtime_arn, session_id?, shell_id?, auth?, reconnect_config?) as shell:
  await shell.send("echo hi\n")
  async for frame in shell:  # frame.channel in ShellChannel.{STDOUT,STDERR}; frame.text
```
State persists across inputs. <=10 shells/runtime. Reconnect: reuse session_id+shell_id (256KB replay).
Frame <=64KB, 250/s, conn <=1h. Needs runtime after 2026-06-05.

Invoke (reasoning): `invoke_agent_runtime(agentRuntimeArn, runtimeSessionId, payload=json.dumps(...).encode(), qualifier)`
-> `resp["response"].read()`.
Stop: `stop_runtime_session(agentRuntimeArn, runtimeSessionId, qualifier)`.
IAM data: `bedrock-agentcore:InvokeAgentRuntime{,Command,CommandShell}`.

## E2B Python surface to mirror  (e2b v2.37, e2b_code_interpreter v2.9)

`Sandbox.create(template?, timeout?, metadata?, envs?, secure=True, allow_internet_access=True, ...)`;
`connect(sandbox_id)`; static+instance `kill`, `set_timeout(timeout)`, `get_info->SandboxInfo`,
`get_metrics`, `list(query,limit,next_token)->paginator`, `is_running()`; context manager; bare
`Sandbox()` deprecated. `get_host(port)` real. Namespaces: `.files .commands .pty .git`.

commands: `run(cmd, background=False, envs?, user="user"|"root", cwd?, on_stdout?, on_stderr?, stdin?,
timeout=60, request_timeout?)` -> fg `CommandResult(stdout,stderr,exit_code,error)`, bg `CommandHandle`
(pid; iterable; `wait(on_stdout,on_stderr,on_pty)->CommandResult`, `kill`, `disconnect`, `send_stdin`,
`close_stdin`). `list()->[ProcessInfo]`, `kill(pid)`, `send_stdin(pid,data)`, `connect(pid)`.
Non-zero exit raises `CommandExitException(stdout,stderr,exit_code,error)`.

files: `read(path, format="text"|"bytes"|"stream")`, `write(path,data)->WriteInfo`,
`write_files([WriteEntry])`, `list(path,depth=1)->[EntryInfo]`, `exists`, `get_info->EntryInfo`,
`remove`, `rename(old,new)->EntryInfo`, `make_dir->bool`, `watch_dir(...)->WatchHandle`. No bulk up/download.
`EntryInfo{name,type,path}`. All take `user?`, `request_timeout?`.

pty: `create(size:PtySize{rows,cols})->CommandHandle`, `send_stdin(pid,bytes)`, `resize(pid,size)`, `kill(pid)`.

run_code (code interpreter): `run_code(code, language?|context?, on_stdout,on_stderr,on_result,on_error,
envs?, timeout?, request_timeout?)->Execution{results:[Result], logs:Logs{stdout:[],stderr:[]},
error:ExecutionError?, execution_count, .text}`. Contexts: `create_code_context(cwd="/home/user",language)->Context{id,language,cwd}`,
`remove/list/restart_code_context`. `Result{is_main_result, _repr_*_, text}`, `ExecutionError{name,value,traceback}`.

AsyncSandbox: everything `async def`+await; `create` async classmethod; dual instance/static
`kill/connect/set_timeout/get_info/...`; `list()` returns async paginator (not awaited);
`AsyncCommandHandle/AsyncWatchHandle/AsyncFileStreamReader`; `async with`.

Exceptions: `SandboxException` base -> `TimeoutException, InvalidArgumentException, NotFoundException
(->FileNotFoundException, SandboxNotFoundException), RateLimitException, TemplateException`;
`AuthenticationException`; add `CommandExitException`, `NotSupportedError`.
