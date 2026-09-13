"""Result/data models mirroring the E2B SDK.

These dataclasses match the field names and semantics of
``e2b`` / ``e2b_code_interpreter`` so callers can read ``.stdout``,
``execution.logs.stdout``, ``execution.text`` etc. exactly as with E2B.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# E2B usernames a command can run as.
Username = Literal["root", "user"]

# Rich display formats a Result may carry (Jupyter MIME types), mirroring E2B.
_RESULT_FORMATS = (
    "text",
    "html",
    "markdown",
    "svg",
    "png",
    "jpeg",
    "pdf",
    "latex",
    "json",
    "javascript",
    "chart",
    "data",
)


@dataclass
class Logs:
    """stdout/stderr captured as lists of chunks (mirrors e2b ``Logs``)."""

    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class ExecutionError:
    """An error raised while executing code (mirrors e2b ``ExecutionError``)."""

    name: str
    value: str
    traceback: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class Result:
    """A single ``run_code`` result value (mirrors e2b ``Result``).

    RaitBox surfaces ``text`` today; the other Jupyter display formats
    (html/png/…) are present for API parity and populated when a backend emits
    them. ``formats()`` and the ``_repr_*_`` accessors match E2B.
    """

    text: str | None = None
    html: str | None = None
    markdown: str | None = None
    svg: str | None = None
    png: str | None = None
    jpeg: str | None = None
    pdf: str | None = None
    latex: str | None = None
    json: Any | None = None
    javascript: str | None = None
    chart: Any | None = None
    data: Any | None = None
    is_main_result: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # E2B parity
        return self.text or ""

    def formats(self) -> list[str]:
        """MIME formats present on this result (E2B ``Result.formats``)."""
        return [f for f in _RESULT_FORMATS if getattr(self, f, None) is not None]

    # E2B/Jupyter display hooks.
    def _repr_html_(self) -> str | None:
        return self.html

    def _repr_markdown_(self) -> str | None:
        return self.markdown

    def _repr_svg_(self) -> str | None:
        return self.svg

    def _repr_png_(self) -> str | None:
        return self.png

    def _repr_jpeg_(self) -> str | None:
        return self.jpeg

    def _repr_pdf_(self) -> str | None:
        return self.pdf

    def _repr_latex_(self) -> str | None:
        return self.latex

    def _repr_json_(self) -> Any | None:
        return self.json

    def _repr_javascript_(self) -> str | None:
        return self.javascript


@dataclass
class Execution:
    """Result of ``run_code`` (mirrors e2b ``Execution``)."""

    results: list[Result] = field(default_factory=list)
    logs: Logs = field(default_factory=Logs)
    error: ExecutionError | None = None
    execution_count: int | None = None

    @property
    def text(self) -> str | None:
        """Text of the main result (E2B semantics)."""
        for r in self.results:
            if r.is_main_result:
                return r.text
        return None

    def to_json(self) -> str:
        return json.dumps(
            {
                "results": [
                    {k: v for k, v in asdict(r).items() if v is not None or k == "text"}
                    for r in self.results
                ],
                "logs": asdict(self.logs),
                "error": asdict(self.error) if self.error else None,
                "execution_count": self.execution_count,
            }
        )


@dataclass
class CommandResult:
    """Result of a foreground ``commands.run`` (mirrors e2b ``CommandResult``)."""

    stdout: str
    stderr: str
    exit_code: int
    error: str | None = None


@dataclass
class EntryInfo:
    """A filesystem entry returned by ``files.list``/``get_info``."""

    name: str
    type: str  # "file" | "dir"
    path: str
    size: int | None = None
    permissions: str | None = None
    mode: int | None = None
    owner: str | None = None
    group: str | None = None


@dataclass
class WriteInfo:
    """Metadata returned by ``files.write`` (mirrors e2b ``WriteInfo``)."""

    name: str
    type: str
    path: str


@dataclass
class SandboxInfo:
    """Metadata about a sandbox (mirrors e2b ``SandboxInfo``)."""

    sandbox_id: str
    template: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    started_at: str | None = None
    state: str | None = None


@dataclass
class PtySize:
    """Terminal size for ``pty.create`` (mirrors e2b ``PtySize``)."""

    rows: int
    cols: int


@dataclass
class ProcessInfo:
    """A running process returned by ``commands.list`` (mirrors e2b)."""

    pid: int
    cmd: str
    tag: str | None = None


@dataclass
class Context:
    """A ``run_code`` execution context (mirrors e2b_code_interpreter ``Context``)."""

    id: str
    language: str = "python"
    cwd: str | None = None


@dataclass
class FilesystemEvent:
    """A filesystem change reported by ``files.watch_dir`` (mirrors e2b).

    ``type`` is one of ``create``, ``remove``, ``write`` (chmod/rename are not
    distinguished by RaitBox's polling watcher).
    """

    name: str
    type: str


@dataclass
class PreviewLink:
    """A reachable URL for a port inside a sandbox (``get_preview_link``).

    RaitBox authenticates previews with a short-lived, port-scoped token sent in
    the ``X-aws-proxy-auth`` header. Attach ``.headers`` to your HTTP request; the
    token expires and can be re-fetched by calling ``get_preview_link`` again.
    """

    url: str
    token: str
    port: int

    @property
    def headers(self) -> dict[str, str]:
        return {"X-aws-proxy-auth": self.token, "X-aws-proxy-port": str(self.port)}


@dataclass
class Volume:
    """A persistent/shared storage volume mountable into sandboxes.

    Backed by AWS EFS (shared, POSIX) or an S3 access point, depending on the
    backend. Create with ``Sandbox.create_volume`` / pass into ``volume_mounts``.
    """

    id: str
    name: str | None = None
    kind: str = "efs"  # "efs" | "s3"


@dataclass
class VolumeMount:
    """A volume attached to a sandbox at ``mount_path`` (subset of an E2B mount)."""

    volume_id: str
    mount_path: str
    subpath: str | None = None
    kind: str = "efs"


@dataclass
class Position:
    """A zero-based ``(line, character)`` position in a document (LSP)."""

    line: int
    character: int


@dataclass
class LspSymbol:
    """A code symbol reported by an LSP server (function, class, variable…).

    ``kind`` is the LSP ``SymbolKind`` integer (1=File, 5=Class, 12=Function…);
    ``location`` is the raw LSP ``Location``/``range`` payload for the symbol.
    """

    name: str
    kind: int
    location: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionItem:
    """A single completion candidate (subset of the LSP ``CompletionItem``)."""

    label: str
    kind: int | None = None
    detail: str | None = None
