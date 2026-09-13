"""Exception hierarchy mirroring the E2B SDK (``e2b.exceptions``), plus
RaitBox/AWS-specific additions.

Keeping the names and inheritance identical to E2B means ``except
SandboxException`` / ``except CommandExitException`` blocks written for E2B
keep working against RaitBox unchanged.
"""

from __future__ import annotations


class SandboxException(Exception):
    """Base for all sandbox errors (mirrors ``e2b.SandboxException``)."""


class TimeoutException(SandboxException):
    """An operation or the sandbox itself timed out."""


class InvalidArgumentException(SandboxException):
    """A caller argument was invalid."""


class NotFoundException(SandboxException):
    """A resource (sandbox, file) was not found."""


class FileNotFoundException(NotFoundException):
    """A file path did not exist in the sandbox filesystem."""


class SandboxNotFoundException(NotFoundException):
    """The referenced sandbox/session does not exist (or was killed)."""


class RateLimitException(SandboxException):
    """The service throttled the request."""


class TemplateException(SandboxException):
    """The requested template/image is invalid or unavailable."""


class NotEnoughSpaceException(SandboxException):
    """The sandbox ran out of disk space (mirrors e2b)."""


class GitUpstreamException(SandboxException):
    """A git operation against the upstream failed (mirrors e2b)."""


class AuthenticationException(Exception):
    """The AWS credential chain is missing, expired, or denied.

    Where E2B would report a rejected API key, RaitBox reports an authentication
    problem with your AWS credentials — so the same ``except`` block applies. Like
    E2B, it extends ``Exception`` rather than ``SandboxException``.
    """


class GitAuthException(AuthenticationException):
    """Git authentication against the upstream failed (mirrors e2b)."""


class BuildException(Exception):
    """A template/image build failed (mirrors e2b; extends ``Exception``)."""


class FileUploadException(BuildException):
    """A file upload during a build failed (mirrors e2b)."""


class VolumeException(Exception):
    """A volume operation failed (mirrors e2b; extends ``Exception``)."""


class NotSupportedError(SandboxException):
    """The selected backend has no equivalent for a requested E2B capability.

    Raised eagerly (instead of a silent no-op) with a message that points at the
    backend that *does* support it — e.g. inbound-port helpers work only on the
    MicroVM backend, so AgentCore backends raise this and suggest reading files.
    """


class CommandExitException(SandboxException):
    """Raised when a command finishes with a non-zero exit status.

    Exposes ``stdout``, ``stderr``, ``exit_code`` and ``error`` so E2B callers
    that inspect those attributes keep working.
    """

    def __init__(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        error: str | None = None,
    ) -> None:
        super().__init__(f"command failed with exit status {exit_code}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error
