"""AgentCore Runtime backend.

Backed by a deployed Bedrock AgentCore *runtime* (an ARM64 container / zip agent
serving ``/invocations`` + ``/ping`` on port 8080). Sessions are pinned by a
caller-generated ``runtimeSessionId`` (>=33 chars).

Command execution uses the one-shot ``invoke_agent_runtime_command`` data-plane
op, which streams ``contentStart``/``contentDelta{stdout,stderr}``/
``contentStop{exitCode,status}`` events. Commands are stateless between calls
(chain with ``&&``). Files are implemented over the shell (``cat``/``base64``),
since Runtime exposes no file API. Interactive PTY is available only on
``AsyncSandbox`` via the ``bedrock-agentcore`` WebSocket shell.

``template`` MUST be an existing ``agentRuntimeArn`` — RaitBox does not
auto-provision runtimes (that requires an ECR image or zip deploy).
"""

from __future__ import annotations

import base64
import shlex
import uuid
from collections.abc import Callable
from typing import Any

import boto3

from ..core.errors import (
    FileNotFoundException,
    InvalidArgumentException,
    SandboxException,
)
from ..core.types import CommandResult, EntryInfo, SandboxInfo, WriteInfo
from .base import Backend

DEFAULT_QUALIFIER = "DEFAULT"


def _new_session_id() -> str:
    # runtimeSessionId must be >=33 chars; two hex UUIDs give 64.
    return uuid.uuid4().hex + uuid.uuid4().hex


class AgentCoreRuntimeBackend(Backend):
    name = "agentcore-runtime"

    def __init__(
        self,
        client: Any,
        control_client: Any,
        runtime_arn: str,
        session_id: str,
        qualifier: str,
        timeout: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._control = control_client
        self._runtime_arn = runtime_arn
        self._session_id = session_id
        self._qualifier = qualifier
        self._timeout = timeout
        self._metadata = dict(metadata or {})
        self._killed = False
        self._env_file: str | None = None
        self._shell_client: Any = None
        self._region_name: str | None = None

    # -- construction ------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        template: str | None,
        timeout: int,
        envs: dict[str, str] | None,
        metadata: dict[str, str] | None,
        region_name: str | None,
        profile_name: str | None,
        client: Any | None = None,
        control_client: Any | None = None,
        shell_client: Any | None = None,
        region_name_shell: str | None = None,
        qualifier: str = DEFAULT_QUALIFIER,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AgentCoreRuntimeBackend:
        if not template:
            raise InvalidArgumentException(
                "agentcore-runtime requires template=<agentRuntimeArn> "
                "(an already-deployed AgentCore runtime)."
            )
        if client is None:
            session = boto3.Session(profile_name=profile_name, region_name=region_name)
            client = session.client("bedrock-agentcore")
        self = cls(
            client,
            control_client,
            template,
            session_id or _new_session_id(),
            qualifier,
            timeout,
            metadata,
        )
        self._shell_client = shell_client
        self._region_name = region_name
        if envs:
            # Persist env for later commands by writing an rc file the helpers source.
            exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in envs.items())
            self.file_write("/tmp/.raitbox_env", exports)
            self._env_file = "/tmp/.raitbox_env"
        else:
            self._env_file = None
        return self

    @classmethod
    def attach(
        cls,
        sandbox_id: str,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        template: str | None = None,
        qualifier: str = DEFAULT_QUALIFIER,
        timeout: int = 300,
        **kwargs: Any,
    ) -> AgentCoreRuntimeBackend:
        if not template:
            raise InvalidArgumentException(
                "agentcore-runtime connect requires template=<agentRuntimeArn>"
            )
        if client is None:
            session = boto3.Session(profile_name=profile_name, region_name=region_name)
            client = session.client("bedrock-agentcore")
        b = cls(client, None, template, sandbox_id, qualifier, timeout)
        b._env_file = None
        return b

    # -- lifecycle ---------------------------------------------------------

    @property
    def sandbox_id(self) -> str:
        return self._session_id

    def kill(self) -> bool:
        if self._killed:
            return False
        try:
            self._client.stop_runtime_session(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=self._session_id,
                qualifier=self._qualifier,
            )
        except Exception as e:
            # AgentCore creates the session lazily on first invoke, so a session
            # that never ran any command has nothing to stop. Treat "not found"
            # as already-stopped rather than failing kill()/context-manager exit.
            if "ResourceNotFoundException" not in type(e).__name__ and "not found" not in str(e):
                raise
        self._killed = True
        return True

    def is_running(self) -> bool:
        return not self._killed

    def get_info(self) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=self._session_id,
            template=self._runtime_arn,
            metadata=self._metadata,
            state="running" if not self._killed else "stopped",
        )

    # -- code execution ----------------------------------------------------

    def run_code(
        self,
        code: str,
        language: str = "python",
        envs: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        from ..core.types import Execution, ExecutionError, Logs, Result

        interp = {"python": "python3", "javascript": "node", "typescript": "ts-node"}.get(language)
        if interp is None:
            raise InvalidArgumentException(f"unsupported language: {language!r}")
        # Feed the code via base64 to avoid quoting issues.
        b64 = base64.b64encode(code.encode()).decode()
        cmd = f"echo {b64} | base64 -d | {interp}"
        result = self.run_command(cmd, envs=envs, on_stdout=on_stdout, on_stderr=on_stderr)
        logs = Logs(
            stdout=[result.stdout] if result.stdout else [],
            stderr=[result.stderr] if result.stderr else [],
        )
        if result.exit_code != 0:
            return Execution(
                results=[],
                logs=logs,
                error=ExecutionError(name="Error", value=result.stderr, traceback=result.stderr),
            )
        results = [Result(text=result.stdout, is_main_result=True)] if result.stdout else []
        return Execution(results=results, logs=logs, error=None)

    def run_command(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        user: str | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if self._killed:
            raise SandboxException("Sandbox is not running (already killed)")
        parts = []
        if getattr(self, "_env_file", None):
            parts.append(f". {self._env_file}")
        if envs:
            parts.extend(f"export {k}={shlex.quote(v)}" for k, v in envs.items())
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)}")
        parts.append(cmd)
        full = " && ".join(parts)

        body: dict[str, Any] = {"command": f"/bin/bash -c {shlex.quote(full)}"}
        if timeout:
            body["timeout"] = int(timeout)
        resp = self._client.invoke_agent_runtime_command(
            agentRuntimeArn=self._runtime_arn,
            runtimeSessionId=self._session_id,
            qualifier=self._qualifier,
            contentType="application/json",
            accept="application/vnd.amazon.eventstream",
            body=body,
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 0
        for event in resp.get("stream", []):
            chunk = event.get("chunk", event)
            if "contentDelta" in chunk:
                delta = chunk["contentDelta"]
                if delta.get("stdout"):
                    stdout_parts.append(delta["stdout"])
                    if on_stdout:
                        on_stdout(delta["stdout"])
                if delta.get("stderr"):
                    stderr_parts.append(delta["stderr"])
                    if on_stderr:
                        on_stderr(delta["stderr"])
            elif "contentStop" in chunk:
                stop = chunk["contentStop"]
                exit_code = stop.get("exitCode", 0)
                if stop.get("status") == "TIMED_OUT":
                    from ..core.errors import TimeoutException

                    raise TimeoutException(f"command timed out: {cmd!r}")
        return CommandResult(
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            exit_code=exit_code,
        )

    # -- interactive shell (async, WebSocket) ------------------------------

    def open_shell(self, *, session_id: str | None = None, shell_id: str | None = None):
        """Return the ``bedrock-agentcore`` async shell context manager.

        Usage (through ``AsyncSandbox.open_shell``)::

            async with await sbx.open_shell() as shell:
                await shell.send("ls\\n")
                async for frame in shell:
                    print(frame.text, end="")

        Requires the ``raitbox[runtime]`` extra (the ``bedrock-agentcore`` SDK).
        """
        client = self._shell_client
        if client is None:
            try:
                from bedrock_agentcore.runtime import AgentCoreRuntimeClient
            except ImportError as e:  # pragma: no cover - optional dep
                from ..core.errors import NotSupportedError

                raise NotSupportedError(
                    "Interactive shell needs the 'runtime' extra: pip install 'raitbox[runtime]'"
                ) from e
            client = AgentCoreRuntimeClient(region=self._region_name)
        return client.open_shell(
            self._runtime_arn,
            session_id=session_id or self._session_id,
            shell_id=shell_id,
        )

    # -- filesystem (over the shell) --------------------------------------

    def file_write(self, path: str, data: str | bytes) -> WriteInfo:
        raw = data.encode() if isinstance(data, str) else data
        b64 = base64.b64encode(raw).decode()
        parent = path.rstrip("/").rpartition("/")[0]
        mkdir = f"mkdir -p {shlex.quote(parent)} && " if parent else ""
        cmd = f"{mkdir}echo {b64} | base64 -d > {shlex.quote(path)}"
        res = self.run_command(cmd)
        if res.exit_code != 0:
            raise SandboxException(f"write failed: {res.stderr}")
        name = path.rstrip("/").rpartition("/")[2]
        return WriteInfo(name=name, type="file", path=path)

    def file_read(self, path: str, format: str = "text") -> str | bytearray:
        res = self.run_command(f"base64 {shlex.quote(path)}")
        if res.exit_code != 0:
            raise FileNotFoundException(f"read failed for {path!r}: {res.stderr}")
        raw = base64.b64decode(res.stdout)
        if format == "text":
            return raw.decode()
        if format in ("bytes", "bytearray"):
            return bytearray(raw)
        raise ValueError(f"unsupported format: {format!r}")

    def file_list(self, path: str = "", depth: int = 1) -> list[EntryInfo]:
        target = path or "."
        # -p appends '/' to directories.
        res = self.run_command(f"ls -1p {shlex.quote(target)}")
        if res.exit_code != 0:
            return []
        entries = []
        for raw in res.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            is_dir = line.endswith("/")
            name = line.rstrip("/")
            entries.append(
                EntryInfo(
                    name=name,
                    type="dir" if is_dir else "file",
                    path=f"{path.rstrip('/')}/{name}" if path else name,
                )
            )
        return entries

    def file_remove(self, path: str) -> None:
        res = self.run_command(f"rm -rf {shlex.quote(path)}")
        if res.exit_code != 0:
            raise SandboxException(f"remove failed: {res.stderr}")

    def file_make_dir(self, path: str) -> bool:
        return self.run_command(f"mkdir -p {shlex.quote(path)}").exit_code == 0

    def file_rename(self, old_path: str, new_path: str) -> EntryInfo:
        res = self.run_command(f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}")
        if res.exit_code != 0:
            raise SandboxException(f"rename failed: {res.stderr}")
        name = new_path.rstrip("/").rpartition("/")[2]
        return EntryInfo(name=name, type="file", path=new_path)
