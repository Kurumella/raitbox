"""Dunefield backend: gVisor sandboxes on EKS reached through a Sandbox Router.

Control calls (create/get/list/timeout/kill) go to ``/v1/sandboxes`` on the Router; data calls use
the same RaitBox agent_server contract as the microvm backend (``/run_code``, ``/run_command``,
``/files/*``, ...), routed by the ``X-Sandbox-ID`` header. Authentication uses the caller's AWS
credentials (Dunefield-IAM); there are no API keys.

Configuration (kwarg > environment variable):
  gateway_url      RAITBOX_DUNEFIELD_GATEWAY_URL     Router URL (Gateway, or a loopback tunnel)
  audience         RAITBOX_DUNEFIELD_AUDIENCE        cell audience; defaults to the gateway URL host
  tls_server_name  RAITBOX_DUNEFIELD_TLS_SERVER_NAME pin TLS name when tunnelling to a Gateway
  ca_bundle        RAITBOX_DUNEFIELD_CA_BUNDLE       CA bundle for privately issued Gateway certs
  template         RAITBOX_DUNEFIELD_TEMPLATE        template when Sandbox.create() gets none
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

from ..core.errors import InvalidArgumentException, SandboxException, SandboxNotFoundException
from ..core.types import PreviewLink, SandboxInfo
from ._agent_server import AgentServerBackend
from ._router_http import RouterHttpClient, iam_token_provider

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _setting(kwargs: dict, name: str) -> str | None:
    return kwargs.get(name) or os.environ.get(f"RAITBOX_DUNEFIELD_{name.upper()}") or None


def _control_client(
    kwargs: dict, region_name: str | None, profile_name: str | None
) -> RouterHttpClient:
    url = _setting(kwargs, "gateway_url")
    if not url:
        raise InvalidArgumentException(
            "dunefield backend requires gateway_url= or RAITBOX_DUNEFIELD_GATEWAY_URL"
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidArgumentException(f"gateway_url must be an http(s) URL with a host: {url!r}")
    tls_server_name = _setting(kwargs, "tls_server_name")
    explicit = _setting(kwargs, "audience") or tls_server_name
    if parsed.hostname in _LOOPBACK and not explicit:
        raise InvalidArgumentException(
            "tunnel mode needs audience= (or tls_server_name=) naming the cell"
        )
    audience = explicit or parsed.hostname
    provider = kwargs.get("token_provider") or iam_token_provider(
        audience, region_name or "us-east-1", profile_name
    )
    return RouterHttpClient(
        url, provider, ca_bundle=_setting(kwargs, "ca_bundle"), tls_server_name=tls_server_name
    )


def _info(sandbox_id: str, data: dict) -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=sandbox_id,
        template=data.get("template"),
        metadata=data.get("metadata") or {},
        started_at=data.get("started_at"),
        state=data.get("state"),
    )


class DunefieldBackend(AgentServerBackend):
    """Agent-server data plane through the Router; lifecycle through ``/v1/sandboxes``.

    Not supported: computer use (no desktop template yet). ``Sandbox.pause()``/``resume()`` work
    as on other backends: the client reads files through the agent and writes the snapshot to S3
    with the caller's own credentials.
    """

    name = "dunefield"

    def __init__(
        self,
        control: RouterHttpClient,
        sandbox_id: str,
        info: dict,
        timeout: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._control = control
        self._sandbox_id = sandbox_id
        self._info = info
        self._timeout = timeout
        self._metadata = dict(metadata or {})
        self._http = control.with_sandbox(sandbox_id)
        self._killed = False

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
        resources: str | None = None,
        ready_timeout: float = 120,
        **kwargs: Any,
    ) -> DunefieldBackend:
        template = template or os.environ.get("RAITBOX_DUNEFIELD_TEMPLATE")
        if not template:
            raise InvalidArgumentException(
                "dunefield backend requires template=<name:version> or RAITBOX_DUNEFIELD_TEMPLATE"
            )
        control = client or _control_client(kwargs, region_name, profile_name)
        body: dict[str, Any] = {
            "template": template,
            "timeout": timeout,
            "envs": envs or {},
            "metadata": metadata or {},
        }
        if resources:
            body["resources"] = resources
        info = control.post("/v1/sandboxes", json=body, timeout=240).json()
        backend = cls(control, info["sandbox_id"], info, timeout, metadata)
        deadline = time.time() + ready_timeout
        while True:
            try:
                backend._http.get("/health", timeout=10)
                return backend
            except SandboxException:
                if time.time() > deadline:
                    backend.kill()
                    raise
                time.sleep(1)

    @classmethod
    def attach(
        cls,
        sandbox_id: str,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        timeout: int = 300,
        **kwargs: Any,
    ) -> DunefieldBackend:
        control = client or _control_client(kwargs, region_name, profile_name)
        info = control.get(f"/v1/sandboxes/{sandbox_id}").json()
        return cls(control, sandbox_id, info, timeout, info.get("metadata"))

    @classmethod
    def list_running(
        cls,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        query: Any = None,
        **kwargs: Any,
    ) -> list[SandboxInfo]:
        control = client or _control_client(kwargs, region_name, profile_name)
        params = {}
        if isinstance(query, dict) and query.get("metadata"):
            k, v = next(iter(query["metadata"].items()))
            params["metadata"] = f"{k}:{v}"
        items = control.get("/v1/sandboxes", params=params).json().get("sandboxes", [])
        return [_info(i["sandbox_id"], i) for i in items]

    # -- lifecycle ---------------------------------------------------------

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    def kill(self) -> bool:
        if self._killed:
            return False
        self._control.request("DELETE", f"/v1/sandboxes/{self._sandbox_id}")
        self._killed = True
        return True

    def is_running(self) -> bool:
        if self._killed:
            return False
        try:
            state = self._control.get(f"/v1/sandboxes/{self._sandbox_id}").json().get("state")
        except SandboxNotFoundException:
            return False
        return state == "READY"

    def get_info(self) -> SandboxInfo:
        return _info(
            self._sandbox_id, self._control.get(f"/v1/sandboxes/{self._sandbox_id}").json()
        )

    def set_timeout(self, timeout: int) -> None:
        self._control.request(
            "PATCH", f"/v1/sandboxes/{self._sandbox_id}", json={"timeout": int(timeout)}
        )

    # -- ports -------------------------------------------------------------

    def get_host(self, port: int) -> str:
        """Base URL for a port served by the Router (needs a Dunefield-IAM token)."""
        return f"{self._control.base_url}/v1/sandboxes/{self._sandbox_id}/ports/{int(port)}"

    def preview_link(self, port: int, expires_in_minutes: int = 30) -> PreviewLink:
        """URL + token for a port. The token is the caller's own short-lived Dunefield-IAM token
        (about 15 minutes, audience-bound); the Router checks sandbox:Ports and ownership."""
        return PreviewLink(
            url=self.get_host(port), token=self._control.authorization(), port=int(port)
        )
