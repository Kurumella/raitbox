# Changelog

All notable changes to RaitBox are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and RaitBox adheres to
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

### Added
- E2B-compatible `Sandbox` and `AsyncSandbox` with `files`, `commands`, `pty`,
  `run_code`, context managers, and the E2B model/exception classes.
- Three pluggable backends: `agentcore-ci` (Bedrock AgentCore Code Interpreter,
  default), `agentcore-runtime` (Bedrock AgentCore Runtime), and `microvm`
  (AWS Lambda MicroVMs).
- Background commands with a real `CommandHandle` (stream/`wait`/`kill`/stdin),
  backed by an in-VM process registry for the `microvm` backend.
- `files.watch_dir` (generic polling `WatchHandle`), `write` overload, and the
  full E2B filesystem surface.
- Static-or-instance `kill`/`set_timeout`/`is_running`/`get_info`, and
  `Sandbox.list()`.
- `pause()`/`resume()` via S3 file snapshots.
- Interactive async shell (`open_shell`) for the `agentcore-runtime` backend.
- In-VM FastAPI agent server + `raitbox.tooling.build_image` to build a MicroVM
  image.
- Cost guardrails: atexit session reclamation and `RAITBOX_MAX_SESSIONS`.
- `run_code(envs=...)` honored on all backends.
- `agentcore-ci`: `files.rename`/`make_dir` (via the session shell), `Sandbox.list()`
  (Code Interpreter sessions), and secret/env injection into `commands.run`.

### Added — extensions beyond the E2B surface (additive)
- Preview links (`get_preview_link`) and web terminals (`get_terminal_link`) — microvm.
- Volumes (`create_volume` / `list_volumes` / `volume_mounts=`, EFS) — microvm.
- Secret injection (`secrets={...}` resolving Secrets Manager / SSM / literals).
- Language servers (`sbx.lsp`), interactive sync PTY (`sbx.pty`), and computer use
  (`sbx.computer_use`) — microvm.
- Warm pools (`WarmPool`), a declarative image builder (`tooling.image.Image`) +
  snapshot registry (`list_images`/`delete_image`), and an MCP server
  (`python -m raitbox.mcp`).
- OpenTelemetry tracing (`observability.configure`), EventBridge webhooks
  (`events.configure`), CloudTrail audit (`audit.lookup_events`), and
  deny-by-default network limits (`internet_access=`).
- `--egress-connector` flag on the image-build CLI.

### Notes
- API shapes validated against the live botocore service models, and all backends
  plus the extensions were exercised end-to-end against a live AWS account.
- Pre-release: APIs may change before 1.0.
