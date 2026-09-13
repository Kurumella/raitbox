"""Backend for Bedrock AgentCore Code Interpreter sessions.

A Code Interpreter session is a stateful IPython kernel with built-in file and
shell tools, reached through a single ``invoke_code_interpreter`` operation whose
response is an event stream. This module adapts that operation to RaitBox's
backend interface, so it becomes the default `run_code` engine.

Two behaviours of the service are worth calling out, because RaitBox smooths them
over for E2B parity:

* the shell tool (``executeCommand``) runs under a pty and returns its combined
  output in the ``stderr`` field — RaitBox re-exposes that as ``stdout``; and
* ``executeCode`` drops the newline ``print`` normally emits — RaitBox re-adds it.

``template`` chooses the interpreter id (default ``aws.codeinterpreter.v1``);
``timeout`` becomes the idle ``sessionTimeoutSeconds`` (the service caps a session
at eight hours). There is no inbound network path, so ``get_host`` is unsupported.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3

from ..core.errors import (
    AuthenticationException,
    FileNotFoundException,
    NotFoundException,
    RateLimitException,
    SandboxException,
)
from ..core.types import (
    CommandResult,
    EntryInfo,
    Execution,
    ExecutionError,
    Logs,
    Result,
    SandboxInfo,
    WriteInfo,
)
from .base import Backend

DEFAULT_IDENTIFIER = "aws.codeinterpreter.v1"


@dataclass
class _Outcome:
    """The flattened result of one Code Interpreter tool invocation."""

    content: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    failed: bool = False


def _raise_for_error_frame(frame: dict[str, Any]) -> None:
    """Translate an AWS error frame into the closest RaitBox exception.

    Control-plane failures arrive as a named member on the stream frame
    (``{"throttlingException": {"message": ...}}``). Rather than enumerate the
    exact names, classify by matching the AWS name — new or renamed errors then
    still land on a sensible class instead of blowing up.
    """
    for member, body in frame.items():
        name = member.lower()
        if not name.endswith("exception"):
            continue
        if "accessdenied" in name or "unauthorized" in name or "forbidden" in name:
            exc: type[Exception] = AuthenticationException
        elif "notfound" in name:
            exc = NotFoundException
        elif "throttl" in name or "toomany" in name or "quota" in name:
            exc = RateLimitException
        else:
            exc = SandboxException
        detail = body.get("message", member) if isinstance(body, dict) else member
        raise exc(detail)
    raise SandboxException(f"unrecognized AgentCore stream frame: {sorted(frame)}")


def _error_from_traceback(traceback_text: str) -> ExecutionError:
    """Recover an ``(exception-name, message)`` pair from an IPython traceback.

    IPython prints the raised exception on the final non-indented line, as
    ``QualifiedName`` or ``QualifiedName: message``; scan upward for it and fall
    back to a generic name if the shape is unexpected.
    """
    exc_name, message = "Error", ""
    for raw in reversed(traceback_text.splitlines()):
        line = raw.strip()
        if not line or raw[:1].isspace():
            continue
        head, _, rest = line.partition(":")
        if head and all(ch.isalnum() or ch in "._" for ch in head):
            exc_name, message = head, rest.strip()
        break
    return ExecutionError(name=exc_name, value=message, traceback=traceback_text)


class AgentCoreCodeInterpreterBackend(Backend):
    name = "agentcore-ci"

    def __init__(
        self,
        client: Any,
        identifier: str,
        session_id: str,
        timeout: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._identifier = identifier
        self._session_id = session_id
        self._timeout = timeout
        self._metadata = dict(metadata or {})
        self._alive = True
        self._fresh_contexts: set[str] = set()
        # Env/secrets seeded at open time, replayed onto shell commands too.
        self._seeded_env: dict[str, str] = {}

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
        **kwargs: Any,
    ) -> AgentCoreCodeInterpreterBackend:
        identifier = template or DEFAULT_IDENTIFIER
        client = client or cls._make_client(region_name, profile_name)
        started = client.start_code_interpreter_session(
            codeInterpreterIdentifier=identifier,
            name=f"raitbox-{uuid.uuid4().hex[:8]}",
            sessionTimeoutSeconds=timeout,
        )
        backend = cls(client, identifier, started["sessionId"], timeout, metadata)
        if envs:
            backend._seeded_env = dict(envs)
            backend._seed_env(envs)
        return backend

    @classmethod
    def attach(
        cls,
        sandbox_id: str,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        template: str | None = None,
        timeout: int = 300,
        **kwargs: Any,
    ) -> AgentCoreCodeInterpreterBackend:
        client = client or cls._make_client(region_name, profile_name)
        return cls(client, template or DEFAULT_IDENTIFIER, sandbox_id, timeout)

    @classmethod
    def list_running(
        cls,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        template: str | None = None,
        **kwargs: Any,
    ) -> list[SandboxInfo]:
        """List active Code Interpreter sessions (drives ``Sandbox.list``)."""
        client = client or cls._make_client(region_name, profile_name)
        identifier = template or DEFAULT_IDENTIFIER
        infos: list[SandboxInfo] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"codeInterpreterIdentifier": identifier, "status": "READY"}
            if token:
                params["nextToken"] = token
            page = client.list_code_interpreter_sessions(**params)
            infos += [
                SandboxInfo(
                    sandbox_id=s["sessionId"],
                    template=s.get("codeInterpreterIdentifier", identifier),
                    state=s.get("status"),
                )
                for s in page.get("items", [])
            ]
            token = page.get("nextToken")
            if not token:
                return infos

    @staticmethod
    def _make_client(region_name: str | None, profile_name: str | None) -> Any:
        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        return session.client("bedrock-agentcore")

    def _seed_env(self, envs: dict[str, str]) -> None:
        """Export vars into the kernel so subsequent code/commands can read them."""
        self._invoke_tool(
            "executeCode",
            {"language": "python", "code": f"import os as _os; _os.environ.update({dict(envs)!r})"},
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def sandbox_id(self) -> str:
        return self._session_id

    def kill(self) -> bool:
        if not self._alive:
            return False
        self._client.stop_code_interpreter_session(
            codeInterpreterIdentifier=self._identifier, sessionId=self._session_id
        )
        self._alive = False
        return True

    def is_running(self) -> bool:
        return self._alive

    def get_info(self) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=self._session_id,
            template=self._identifier,
            metadata=self._metadata,
            state="running" if self._alive else "stopped",
        )

    # -- execution contexts ------------------------------------------------

    def create_code_context(self, cwd: str | None = None, language: str = "python"):
        from ..core.types import Context

        ctx = Context(id=uuid.uuid4().hex, language=language, cwd=cwd)
        self._fresh_contexts.add(ctx.id)
        return ctx

    def restart_code_context(self, context) -> None:
        # The next run in this context clears kernel state (clearContext).
        self._fresh_contexts.add(context.id)

    def remove_code_context(self, context) -> None:
        self._fresh_contexts.discard(context.id)

    # -- code execution ----------------------------------------------------

    def run_code(
        self,
        code: str,
        language: str = "python",
        context: Any = None,
        envs: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> Execution:
        prelude = ""
        clear = False
        if context is not None:
            clear = context.id in self._fresh_contexts
            self._fresh_contexts.discard(context.id)
            if context.cwd:
                prelude += f"import os as _os; _os.chdir({context.cwd!r})\n"
        if envs and language == "python":
            prelude += f"import os as _os; _os.environ.update({dict(envs)!r})\n"

        args: dict[str, Any] = {"code": prelude + code, "language": language}
        if clear:
            args["clearContext"] = True
        outcome = self._invoke_tool("executeCode", args)

        # executeCode strips print()'s trailing newline; re-add it for parity.
        stdout = outcome.stdout
        if stdout and not stdout.endswith("\n"):
            stdout += "\n"

        if outcome.failed:
            if on_stderr and outcome.stderr:
                on_stderr(outcome.stderr)
            logs = Logs(stdout=[stdout] if stdout else [], stderr=[])
            return Execution(results=[], logs=logs, error=_error_from_traceback(outcome.stderr))

        if on_stdout and stdout:
            on_stdout(stdout)
        logs = Logs(
            stdout=[stdout] if stdout else [],
            stderr=[outcome.stderr] if outcome.stderr else [],
        )
        head_text = outcome.content[0].get("text") if outcome.content else None
        results = [Result(text=head_text)] if head_text is not None else []
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
        line = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
        # Replay seeded secrets/env as an inline prefix (per-call envs win).
        effective_env = {**self._seeded_env, **(envs or {})}
        if effective_env:
            assigns = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in effective_env.items())
            line = f"{assigns} {line}"
        outcome = self._invoke_tool("executeCommand", {"command": line})
        # The pty merges both streams into stderr; present it as stdout.
        combined = outcome.stderr or outcome.stdout
        if on_stdout and combined:
            on_stdout(combined)
        return CommandResult(stdout=combined, stderr=outcome.stderr, exit_code=outcome.exit_code)

    # -- filesystem --------------------------------------------------------

    def file_write(self, path: str, data: str | bytes) -> WriteInfo:
        entry = (
            {"path": path, "text": data} if isinstance(data, str) else {"path": path, "blob": data}
        )
        outcome = self._invoke_tool("writeFiles", {"content": [entry]})
        if outcome.failed:
            raise SandboxException(f"could not write {path!r}: {outcome.content}")
        return WriteInfo(name=_leaf(path), type="file", path=path)

    def file_read(self, path: str, format: str = "text") -> str | bytearray:
        outcome = self._invoke_tool("readFiles", {"paths": [path]})
        if outcome.failed or not outcome.content:
            raise FileNotFoundException(f"could not read {path!r}")
        resource = outcome.content[0].get("resource", {})
        payload = resource.get("text", resource.get("blob"))
        if payload is None:
            raise SandboxException(f"unexpected read payload for {path!r}")
        if format == "text":
            return payload.decode() if isinstance(payload, bytes) else payload
        if format in ("bytes", "bytearray"):
            return bytearray(payload.encode() if isinstance(payload, str) else payload)
        raise ValueError(f"unsupported read format: {format!r}")

    def file_list(self, path: str = "", depth: int = 1) -> list[EntryInfo]:
        outcome = self._invoke_tool("listFiles", {"directoryPath": path})
        entries: list[EntryInfo] = []
        for item in outcome.content:
            uri = item.get("uri", "")
            kind = "dir" if item.get("description") == "Directory" else "file"
            entries.append(
                EntryInfo(name=item.get("name", ""), type=kind, path=uri.removeprefix("file:///"))
            )
        return entries

    def file_remove(self, path: str) -> None:
        outcome = self._invoke_tool("removeFiles", {"paths": [path]})
        if outcome.failed:
            raise SandboxException(f"could not remove {path!r}: {outcome.content}")

    def file_make_dir(self, path: str) -> bool:
        # No native mkdir tool — go through the session shell.
        return not self._invoke_tool(
            "executeCommand", {"command": f"mkdir -p {shlex.quote(path)}"}
        ).failed

    def file_rename(self, old_path: str, new_path: str) -> EntryInfo:
        # No native rename tool — go through the session shell.
        outcome = self._invoke_tool(
            "executeCommand", {"command": f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}"}
        )
        if outcome.failed:
            raise SandboxException(
                f"could not rename {old_path!r} -> {new_path!r}: {outcome.stderr}"
            )
        return EntryInfo(name=_leaf(new_path), type="file", path=new_path)

    # -- transport ---------------------------------------------------------

    def _invoke_tool(self, tool: str, arguments: dict[str, Any]) -> _Outcome:
        """Run one Code Interpreter tool and reduce its event stream."""
        if not self._alive:
            raise SandboxException("sandbox already stopped")
        stream = self._client.invoke_code_interpreter(
            codeInterpreterIdentifier=self._identifier,
            sessionId=self._session_id,
            name=tool,
            arguments=arguments,  # AgentCore expects a plain dict, not JSON text
        )["stream"]
        out = _Outcome()
        structured: dict[str, Any] = {}
        for frame in stream:
            result = frame.get("result")
            if result is None:
                _raise_for_error_frame(frame)
                continue
            out.content += result.get("content", [])
            structured.update(result.get("structuredContent", {}))
            out.failed = out.failed or bool(result.get("isError"))
        out.stdout = structured.get("stdout", "")
        out.stderr = structured.get("stderr", "")
        out.exit_code = int(structured.get("exitCode", 0))
        return out


def _leaf(path: str) -> str:
    return path.rstrip("/").rpartition("/")[2]
