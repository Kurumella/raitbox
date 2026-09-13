"""RaitBox — E2B-compatible agent sandbox SDK backed by AWS.

Sandboxes run in your own AWS account on one of three backends (selected via
``backend=`` or ``RAITBOX_BACKEND``):

  - ``agentcore-ci``       Bedrock AgentCore Code Interpreter (default; best run_code)
  - ``agentcore-runtime``  Bedrock AgentCore Runtime (best commands + shell)
  - ``microvm``            AWS Lambda MicroVMs (full-OS Firecracker VMs)

Drop-in usage::

    # from e2b_code_interpreter import Sandbox    # E2B
    from raitbox import Sandbox                    # RaitBox

    with Sandbox() as sbx:
        print(sbx.run_code("print('hi')").logs.stdout)
"""

from __future__ import annotations

from .client.aio import AsyncSandbox
from .client.sync import Sandbox
from .core.errors import (
    AuthenticationException,
    BuildException,
    CommandExitException,
    FileNotFoundException,
    FileUploadException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
    NotEnoughSpaceException,
    NotFoundException,
    NotSupportedError,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    TemplateException,
    TimeoutException,
    VolumeException,
)
from .core.handles import (
    CommandHandle,
    Git,
    LspServer,
    PtyHandle,
    SandboxPaginator,
    WatchHandle,
)
from .core.types import (
    CommandResult,
    CompletionItem,
    Context,
    EntryInfo,
    Execution,
    ExecutionError,
    FilesystemEvent,
    Logs,
    LspSymbol,
    Position,
    PreviewLink,
    ProcessInfo,
    PtySize,
    Result,
    SandboxInfo,
    Volume,
    VolumeMount,
    WriteInfo,
)
from .core.warm_pool import WarmPool

__all__ = [
    "AsyncSandbox",
    "AuthenticationException",
    "BuildException",
    "CommandExitException",
    "CommandHandle",
    "CommandResult",
    "CompletionItem",
    "Context",
    "EntryInfo",
    "Execution",
    "ExecutionError",
    "FileNotFoundException",
    "FileUploadException",
    "FilesystemEvent",
    "Git",
    "GitAuthException",
    "GitUpstreamException",
    "InvalidArgumentException",
    "Logs",
    "LspServer",
    "LspSymbol",
    "NotEnoughSpaceException",
    "NotFoundException",
    "NotSupportedError",
    "Position",
    "PreviewLink",
    "ProcessInfo",
    "PtyHandle",
    "PtySize",
    "RateLimitException",
    "Result",
    "Sandbox",
    "SandboxException",
    "SandboxInfo",
    "SandboxNotFoundException",
    "SandboxPaginator",
    "TemplateException",
    "TimeoutException",
    "Volume",
    "VolumeException",
    "VolumeMount",
    "WarmPool",
    "WatchHandle",
    "WriteInfo",
]

__version__ = "0.1.0"
