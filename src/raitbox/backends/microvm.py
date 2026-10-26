"""Lambda MicroVM backend.

Launches a Firecracker MicroVM from a prebuilt image (``template`` = the image
ARN) via the ``lambda-microvms`` boto3 client, then talks to the RaitBox agent
server baked into that image over the VM's HTTPS endpoint.

Lifecycle: ``run_microvm`` -> poll ``get_microvm`` until ``RUNNING`` -> mint an
auth token -> issue HTTP calls. ``terminate_microvm`` on kill; suspend/resume is
handled automatically by the image ``idlePolicy`` (``autoResumeEnabled``).

Note: the ``lambda-microvms`` client requires a recent boto3 (>=1.43); older
Lambda/runtime-bundled boto3 does not include it.
"""

from __future__ import annotations

import time
from typing import Any

import boto3

from ..core.errors import (
    InvalidArgumentException,
    SandboxException,
    TemplateException,
    TimeoutException,
)
from ..core.types import (
    SandboxInfo,
)
from ._agent_server import AgentServerBackend
from ._microvm_http import MicroVMHttpClient

_ALIVE_STATES = {"RUNNING", "SUSPENDED"}
_DEAD_STATES = {"TERMINATED", "TERMINATING", "FAILED"}


class MicroVMBackend(AgentServerBackend):
    name = "microvm"

    def __init__(
        self,
        client: Any,
        microvm_id: str,
        endpoint: str,
        template: str,
        http: MicroVMHttpClient,
        timeout: int,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._microvm_id = microvm_id
        self._endpoint = endpoint
        self._template = template
        self._http = http
        self._timeout = timeout
        self._metadata = dict(metadata or {})
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
        execution_role_arn: str | None = None,
        ingress_connectors: list[str] | None = None,
        egress_connectors: list[str] | None = None,
        idle_policy: dict[str, Any] | None = None,
        app_port: int = 8080,
        token_ttl_minutes: int = 30,
        ready_timeout: float = 120,
        volume_mounts: list[Any] | None = None,
        internet_access: bool | None = None,
        **kwargs: Any,
    ) -> MicroVMBackend:
        if not template:
            raise TemplateException(
                "microvm backend requires template=<microvm-image ARN>. "
                "Build one with raitbox.tooling.build_image."
            )
        # Network limits: egress is deny-by-default (no connectors = no egress).
        # internet_access is an explicit guard rail on top of that.
        if internet_access is False:
            egress_connectors = []
        elif internet_access is True and not egress_connectors:
            raise InvalidArgumentException(
                "internet_access=True needs egress_connectors=[...]; the MicroVM "
                "reaches the network only through a VPC egress connector."
            )
        if client is None:
            session = boto3.Session(profile_name=profile_name, region_name=region_name)
            client = session.client("lambda-microvms")

        run_params: dict[str, Any] = {
            "imageIdentifier": template,
            "maximumDurationInSeconds": min(max(timeout, 1), 28800),
            "idlePolicy": idle_policy
            or {
                "autoResumeEnabled": True,
                "maxIdleDurationSeconds": min(max(timeout, 1), 28800),
                "suspendedDurationSeconds": 3600,
            },
        }
        if execution_role_arn:
            run_params["executionRoleArn"] = execution_role_arn
        if ingress_connectors:
            run_params["ingressNetworkConnectors"] = ingress_connectors
        if egress_connectors:
            run_params["egressNetworkConnectors"] = egress_connectors
        # The /run hook applies env vars and mounts EFS volumes inside the VM.
        payload: dict[str, Any] = {}
        if envs:
            payload["envs"] = envs
        if volume_mounts:
            payload["mounts"] = [
                {
                    "file_system_id": m.volume_id,
                    "mount_path": m.mount_path,
                    "subpath": getattr(m, "subpath", None),
                    "region": region_name,
                }
                for m in volume_mounts
            ]
        if payload:
            import json

            run_params["runHookPayload"] = json.dumps(payload)

        resp = client.run_microvm(**run_params)
        microvm_id = resp["microvmId"]
        endpoint = resp["endpoint"]

        cls._wait_running(client, microvm_id, ready_timeout)

        def token_provider() -> tuple[str, float]:
            tok = client.create_microvm_auth_token(
                microvmIdentifier=microvm_id,
                expirationInMinutes=token_ttl_minutes,
                allowedPorts=[{"allPorts": {}}],
            )
            token = tok["authToken"]["X-aws-proxy-auth"]
            return token, time.time() + token_ttl_minutes * 60

        http = MicroVMHttpClient(endpoint, token_provider, port=app_port)
        cls._wait_healthy(http, ready_timeout)
        return cls(client, microvm_id, endpoint, template, http, timeout, metadata)

    @classmethod
    def attach(
        cls,
        sandbox_id: str,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        template: str | None = None,
        app_port: int = 8080,
        token_ttl_minutes: int = 30,
        timeout: int = 300,
        **kwargs: Any,
    ) -> MicroVMBackend:
        if client is None:
            session = boto3.Session(profile_name=profile_name, region_name=region_name)
            client = session.client("lambda-microvms")
        detail = client.get_microvm(microvmIdentifier=sandbox_id)
        if detail.get("state") not in _ALIVE_STATES:
            raise SandboxException(f"MicroVM {sandbox_id} is not alive: {detail.get('state')}")
        endpoint = detail["endpoint"]

        def token_provider() -> tuple[str, float]:
            tok = client.create_microvm_auth_token(
                microvmIdentifier=sandbox_id,
                expirationInMinutes=token_ttl_minutes,
                allowedPorts=[{"allPorts": {}}],
            )
            return tok["authToken"]["X-aws-proxy-auth"], time.time() + token_ttl_minutes * 60

        http = MicroVMHttpClient(endpoint, token_provider, port=app_port)
        tmpl = template or detail.get("imageIdentifier", "")
        return cls(client, sandbox_id, endpoint, tmpl, http, timeout)

    @classmethod
    def list_running(
        cls,
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        client: Any | None = None,
        **kwargs: Any,
    ) -> list[SandboxInfo]:
        if client is None:
            session = boto3.Session(profile_name=profile_name, region_name=region_name)
            client = session.client("lambda-microvms")
        infos: list[SandboxInfo] = []
        next_token: str | None = None
        while True:
            params = {"nextToken": next_token} if next_token else {}
            resp = client.list_microvms(**params)
            for m in resp.get("items", []):
                if m.get("state") in _ALIVE_STATES:
                    infos.append(
                        SandboxInfo(
                            sandbox_id=m["microvmId"],
                            template=m.get("imageArn"),
                            state=m.get("state"),
                        )
                    )
            next_token = resp.get("nextToken")
            if not next_token:
                return infos

    @staticmethod
    def _wait_running(client: Any, microvm_id: str, deadline_s: float) -> None:
        end = time.time() + deadline_s
        while time.time() < end:
            state = client.get_microvm(microvmIdentifier=microvm_id).get("state")
            if state == "RUNNING":
                return
            if state in _DEAD_STATES:
                raise SandboxException(f"MicroVM entered {state} before RUNNING")
            time.sleep(2)
        raise TimeoutException(f"MicroVM {microvm_id} did not reach RUNNING in time")

    @staticmethod
    def _wait_healthy(http: MicroVMHttpClient, deadline_s: float) -> None:
        end = time.time() + deadline_s
        last: Exception | None = None
        while time.time() < end:
            try:
                http.get("/health", timeout=10)
                return
            except Exception as e:  # 502 while resuming/starting, etc.
                last = e
                time.sleep(2)
        raise TimeoutException(f"MicroVM agent server not healthy in time: {last}")

    # -- lifecycle ---------------------------------------------------------

    @property
    def sandbox_id(self) -> str:
        return self._microvm_id

    def kill(self) -> bool:
        if self._killed:
            return False
        self._client.terminate_microvm(microvmIdentifier=self._microvm_id)
        self._killed = True
        return True

    def is_running(self) -> bool:
        if self._killed:
            return False
        state = self._client.get_microvm(microvmIdentifier=self._microvm_id).get("state")
        return state in _ALIVE_STATES

    def get_info(self) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=self._microvm_id,
            template=self._template,
            metadata=self._metadata,
            state="running" if not self._killed else "stopped",
        )

    def get_host(self, port: int) -> str:
        """MicroVMs DO expose a network path — return the endpoint host."""
        return self._endpoint

    def preview_link(self, port: int, expires_in_minutes: int = 30):
        """A reachable URL + short-lived, port-scoped token for a port in the VM."""
        from ..core.types import PreviewLink

        tok = self._client.create_microvm_auth_token(
            microvmIdentifier=self._microvm_id,
            expirationInMinutes=expires_in_minutes,
            allowedPorts=[{"port": port}],
        )
        return PreviewLink(
            url=f"https://{self._endpoint}",
            token=tok["authToken"]["X-aws-proxy-auth"],
            port=port,
        )

    # -- computer use ------------------------------------------------------

    def computer_use(self, action: str, params: dict[str, Any]) -> Any:
        return (
            self._http.post("/computer", json={"action": action, "params": params})
            .json()
            .get("result")
        )
