"""Dunefield backend: gVisor sandboxes on EKS reached through a Sandbox Router.

Control calls (create/get/list/timeout/kill) go to ``/v1/sandboxes`` on the Router; data calls use
the same RaitBox agent_server contract as the microvm backend (``/run_code``, ``/run_command``,
``/files/*``, ...), routed by the ``X-Sandbox-ID`` header. Authentication uses the caller's AWS
credentials (Dunefield-IAM); there are no API keys.

Configuration (kwarg > environment variable):
  gateway_url      RAITBOX_DUNEFIELD_GATEWAY_URL     Router URL (Gateway, or a loopback tunnel)
  audience         RAITBOX_DUNEFIELD_AUDIENCE        cell audience; defaults to the gateway URL host
  tls_server_name  RAITBOX_DUNEFIELD_TLS_SERVER_NAME pin TLS name when tunnelling to a Gateway
  ca_bundle        RAITBOX_DUNEFIELD_CA_BUNDLE       CA bundle for privately issued Gateway certificates
  template         RAITBOX_DUNEFIELD_TEMPLATE        default template when Sandbox.create() gets none
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

from ..core.errors import InvalidArgumentException, NotSupportedError, SandboxException, SandboxNotFoundException
from ..core.types import SandboxInfo
from ._router_http import RouterHttpClient, iam_token_provider
from .microvm import MicroVMBackend


def _setting(kwargs: dict, name: str) -> str | None:
    return kwargs.get(name) or os.environ.get(f"RAITBOX_DUNEFIELD_{name.upper()}") or None


def _control_client(kwargs: dict, region_name: str | None, profile_name: str | None) -> RouterHttpClient:
    url = _setting(kwargs, "gateway_url")
    if not url:
        raise InvalidArgumentException("dunefield backend requires gateway_url= or RAITBOX_DUNEFIELD_GATEWAY_URL")
    audience = _setting(kwargs, "audience") or _setting(kwargs, "tls_server_name") or urlparse(url).hostname
    host = urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1") and not (_setting(kwargs, "audience") or _setting(kwargs, "tls_server_name")):
        raise InvalidArgumentException("tunnel mode needs audience= (or tls_server_name=) naming the cell")
    provider = kwargs.get("token_provider") or iam_token_provider(audience, region_name or "us-east-1", profile_name)
    return RouterHttpClient(url, provider, ca_bundle=_setting(kwargs, "ca_bundle"),
                            tls_server_name=_setting(kwargs, "tls_server_name"))


class DunefieldBackend(MicroVMBackend):
    name = "dunefield"

    def __init__(self, control: RouterHttpClient, sandbox_id: str, info: dict, timeout: int,
                 metadata: dict[str, str] | None = None) -> None:
        self._control = control
        self._info = info
        super().__init__(client=None, microvm_id=sandbox_id, endpoint=control.base_url,
                         template=info.get("template") or "", http=control.with_sandbox(sandbox_id),
                         timeout=timeout, metadata=metadata)

    @classmethod
    def open(cls, *, template: str | None, timeout: int, envs: dict[str, str] | None,
             metadata: dict[str, str] | None, region_name: str | None, profile_name: str | None,
             client: Any | None = None, resources: str | None = None, ready_timeout: float = 120,
             **kwargs: Any) -> DunefieldBackend:
        template = template or os.environ.get("RAITBOX_DUNEFIELD_TEMPLATE")
        if not template:
            raise InvalidArgumentException("dunefield backend requires template=<name:version> or RAITBOX_DUNEFIELD_TEMPLATE")
        control = client or _control_client(kwargs, region_name, profile_name)
        body = {"template": template, "timeout": timeout, "envs": envs or {}, "metadata": metadata or {}}
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
    def attach(cls, sandbox_id: str, *, region_name: str | None = None, profile_name: str | None = None,
               client: Any | None = None, timeout: int = 300, **kwargs: Any) -> DunefieldBackend:
        control = client or _control_client(kwargs, region_name, profile_name)
        info = control.get(f"/v1/sandboxes/{sandbox_id}").json()
        return cls(control, sandbox_id, info, timeout)

    @classmethod
    def list_running(cls, *, region_name: str | None = None, client: Any | None = None, query: Any = None,
                     profile_name: str | None = None, **kwargs: Any) -> list[SandboxInfo]:
        control = client or _control_client(kwargs, region_name, profile_name)
        params = {}
        if isinstance(query, dict) and query.get("metadata"):
            k, v = next(iter(query["metadata"].items()))
            params["metadata"] = f"{k}:{v}"
        items = control.get("/v1/sandboxes", params=params).json().get("sandboxes", [])
        return [SandboxInfo(sandbox_id=i["sandbox_id"], template=i.get("template"), metadata=i.get("metadata") or {},
                            started_at=i.get("started_at"), state=i.get("state")) for i in items]

    def kill(self) -> bool:
        if self._killed:
            return False
        self._control.request("DELETE", f"/v1/sandboxes/{self._microvm_id}")
        self._killed = True
        return True

    def is_running(self) -> bool:
        if self._killed:
            return False
        try:
            return self._control.get(f"/v1/sandboxes/{self._microvm_id}").json().get("state") == "READY"
        except SandboxNotFoundException:
            return False

    def get_info(self) -> SandboxInfo:
        i = self._control.get(f"/v1/sandboxes/{self._microvm_id}").json()
        return SandboxInfo(sandbox_id=self._microvm_id, template=i.get("template"), metadata=i.get("metadata") or {},
                           started_at=i.get("started_at"), state=i.get("state"))

    def set_timeout(self, timeout: int) -> None:
        self._control.request("PATCH", f"/v1/sandboxes/{self._microvm_id}", json={"timeout": int(timeout)})

    def get_host(self, port: int) -> str:
        """Base URL for a port in the sandbox, served by the Sandbox Router (needs a Dunefield-IAM token)."""
        return f"{self._control.base_url.rstrip('/')}/v1/sandboxes/{self._microvm_id}/ports/{int(port)}"

    def preview_link(self, port: int, expires_in_minutes: int = 30):
        """URL + headers for a port. The token is the caller's own short-lived Dunefield-IAM token
        (about 15 minutes, audience-bound); the Router checks sandbox:Ports and ownership."""
        from ..core.types import PreviewLink

        return PreviewLink(url=self.get_host(port), token=self._control._auth(), port=int(port))
