"""HTTP transport to a Dunefield Sandbox Router.

Every request carries a Dunefield-IAM token: an STS GetCallerIdentity request presigned with the
caller's AWS credentials and a signed ``x-dunefield-audience`` header naming the target cell. The
Router replays it to STS, so no API keys are involved. Tokens are cached and refreshed before
expiry.
Data-plane requests also carry ``X-Sandbox-ID``.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from ..core.errors import (
    AuthenticationException,
    InvalidArgumentException,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    TemplateException,
    TimeoutException,
)

AUDIENCE_HEADER = "x-dunefield-audience"
_TOKEN_TTL = 900
_REFRESH_MARGIN = 300


def iam_token_provider(
    audience: str,
    region_name: str = "us-east-1",
    profile_name: str | None = None,
    session: Any = None,
) -> Callable[[], tuple[str, float]]:
    """Return a callable producing (token, expiry_epoch) for the given audience."""
    import boto3

    sess = session or boto3.Session(profile_name=profile_name, region_name=region_name)
    sts = sess.client(
        "sts", region_name=region_name, endpoint_url=f"https://sts.{region_name}.amazonaws.com"
    )

    def add_audience(request, **_kwargs):
        request.headers[AUDIENCE_HEADER] = audience

    sts.meta.events.register("before-sign.sts.GetCallerIdentity", add_audience)

    def provide() -> tuple[str, float]:
        url = sts.generate_presigned_url(
            "get_caller_identity", Params={}, ExpiresIn=_TOKEN_TTL, HttpMethod="GET"
        )
        token = "v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return token, time.time() + _TOKEN_TTL

    return provide


class _ServerNameAdapter(HTTPAdapter):
    """Verify TLS against a pinned server name (for loopback tunnels to a cell's Gateway)."""

    def __init__(self, server_hostname: str, **kw: Any) -> None:
        self._server_hostname = server_hostname
        super().__init__(**kw)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["server_hostname"] = self._server_hostname
        kwargs["assert_hostname"] = self._server_hostname
        super().init_poolmanager(*args, **kwargs)


class RouterHttpClient:
    """Signed HTTP transport to one Sandbox Router (implements ``AgentHttp``)."""

    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], tuple[str, float]],
        *,
        sandbox_id: str | None = None,
        ca_bundle: str | None = None,
        tls_server_name: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sandbox_id = sandbox_id
        self._token_provider = token_provider
        self._token: str | None = None
        self._expiry = 0.0
        self._session = requests.Session()
        self._verify: bool | str = ca_bundle or True
        self._host_header: str | None = None
        if tls_server_name and self.base_url.startswith("https://"):
            # Tunnelled to a Gateway: verify TLS and route (Host header) as the cell hostname.
            self._session.mount("https://", _ServerNameAdapter(tls_server_name))
            self._host_header = tls_server_name

    def with_sandbox(self, sandbox_id: str) -> RouterHttpClient:
        clone = RouterHttpClient.__new__(RouterHttpClient)
        clone.__dict__.update(self.__dict__)
        clone.sandbox_id = sandbox_id
        return clone

    def authorization(self) -> str:
        """The ``Authorization`` header value (a cached, refreshed Dunefield-IAM token)."""
        if self._token is None or time.time() >= self._expiry - _REFRESH_MARGIN:
            self._token, self._expiry = self._token_provider()
        return f"Dunefield-IAM {self._token}"

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        stream: bool = False,
        timeout: float = 300,
        params: dict | None = None,
    ) -> requests.Response:
        headers = {"Authorization": self.authorization(), "Content-Type": "application/json"}
        if self.sandbox_id:
            headers["X-Sandbox-ID"] = self.sandbox_id
        if self._host_header:
            headers["Host"] = self._host_header
        try:
            resp = self._session.request(
                method,
                self.base_url + path,
                json=json,
                params=params,
                headers=headers,
                stream=stream,
                timeout=timeout,
                verify=self._verify,
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise TimeoutException(f"request to the Sandbox Router timed out: {path}") from exc
        except requests.RequestException as exc:
            raise SandboxException(
                f"cannot reach the Sandbox Router at {self.base_url}: {exc.__class__.__name__}"
            ) from exc
        self._raise_for_status(resp)
        return resp

    def post(self, path: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("POST", path, json=json, **kw)

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.request("GET", path, **kw)

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            msg = (
                resp.json().get("message")
                or resp.json().get("detail")
                or resp.json().get("error")
                or ""
            )
        except ValueError:
            msg = resp.text[:200]
        code = resp.status_code
        if code in (401, 403):
            raise AuthenticationException(f"Dunefield denied the request ({code}): {msg}")
        if code == 404:
            raise SandboxNotFoundException(f"not found: {msg}")
        if code == 409:
            raise TemplateException(f"conflict: {msg}")
        if code == 400:
            raise InvalidArgumentException(msg or "invalid request")
        if code == 429:
            raise RateLimitException(f"rate limited: {msg}")
        if code == 504:
            raise TimeoutException(f"timed out: {msg}")
        raise SandboxException(f"Dunefield error {code}: {msg}")
