"""HTTP client to a Lambda MicroVM's per-VM HTTPS endpoint.

MicroVMs expose no exec API; instead you talk to the app baked into the image
(RaitBox ships that app as ``raitbox.agent_server``). Requests carry the
``X-aws-proxy-auth`` token minted by ``create_microvm_auth_token`` and an
``X-aws-proxy-port`` selecting the app port. Tokens are refreshed before expiry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests

from ..core.errors import (
    AuthenticationException,
    RateLimitException,
    SandboxException,
)

# Refresh the auth token this many seconds before it expires.
_REFRESH_MARGIN = 60


class MicroVMHttpClient:
    """Signed HTTP transport to one MicroVM endpoint."""

    def __init__(
        self,
        endpoint: str,
        token_provider: Callable[[], tuple[str, float]],
        port: int = 8080,
    ) -> None:
        self._endpoint = endpoint
        self._port = port
        # Returns (token, expiry_epoch_seconds).
        self._token_provider = token_provider
        self._token: str | None = None
        self._expiry: float = 0.0
        self._session = requests.Session()

    def _auth_token(self) -> str:
        now = time.time()
        if self._token is None or now >= self._expiry - _REFRESH_MARGIN:
            self._token, self._expiry = self._token_provider()
        return self._token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "X-aws-proxy-auth": self._auth_token(),
            "X-aws-proxy-port": str(self._port),
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        stream: bool = False,
        timeout: float = 300,
    ) -> requests.Response:
        url = f"https://{self._endpoint}{path}"
        resp = self._session.request(
            method,
            url,
            json=json,
            headers=self._headers(),
            stream=stream,
            timeout=timeout,
        )
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
        if resp.status_code in (401, 403):
            raise AuthenticationException(
                f"MicroVM endpoint denied request ({resp.status_code}): "
                "auth token invalid/expired or port not allowed."
            )
        if resp.status_code == 429:
            raise RateLimitException("MicroVM endpoint rate limited (429)")
        if resp.status_code == 502:
            raise SandboxException(
                "MicroVM app not responding (502): the guest may still be starting/resuming."
            )
        raise SandboxException(f"MicroVM endpoint error {resp.status_code}: {resp.text[:500]}")
