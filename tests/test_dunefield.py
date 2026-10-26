"""Unit tests for the dunefield backend (no AWS or network access)."""

import base64
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from raitbox import Sandbox
from raitbox.backends._router_http import iam_token_provider
from raitbox.core.errors import InvalidArgumentException


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeRouter:
    """Stands in for RouterHttpClient: records calls and answers like the Sandbox Router."""

    base_url = "http://127.0.0.1:8443"

    def __init__(self):
        self.calls = []
        self.sandbox_id = None

    def with_sandbox(self, sid):
        clone = FakeRouter()
        clone.calls = self.calls
        clone.sandbox_id = sid
        return clone

    def request(self, method, path, json=None, **kw):
        self.calls.append((method, path, self.sandbox_id, json))
        if path == "/v1/sandboxes" and method == "POST":
            return FakeResp(
                {"sandbox_id": "sbx-test-1", "state": "READY", "template": json["template"]}
            )
        if path == "/run_code":
            return FakeResp({"stdout": "42\n", "stderr": "", "results": []})
        if path == "/run_command":
            return FakeResp({"stdout": "hi\n", "stderr": "", "exit_code": 0})
        return FakeResp({"ok": True})

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def authorization(self):
        return "Dunefield-IAM v1.token"


def test_lifecycle_through_router():
    router = FakeRouter()
    with Sandbox.create(
        template="python-agent:v1", backend="dunefield", client=router, timeout=600
    ) as sbx:
        assert sbx.sandbox_id == "sbx-test-1"
        assert sbx.run_code("print(6*7)").logs.stdout == ["42\n"]
        assert sbx.commands.run("echo hi").stdout == "hi\n"
    methods = [(m, p, s) for m, p, s, _ in router.calls]
    assert ("POST", "/v1/sandboxes", None) in methods
    assert ("POST", "/run_code", "sbx-test-1") in methods  # data calls carry the sandbox id
    assert (
        "DELETE",
        "/v1/sandboxes/sbx-test-1",
        None,
    ) in methods  # context exit deletes the sandbox


def test_aliases_resolve_to_dunefield():
    from raitbox.core.settings import resolve_backend

    assert {resolve_backend(n) for n in ("dunefield", "gvisor")} == {"dunefield"}


def test_tunnel_requires_audience(monkeypatch):
    monkeypatch.delenv("RAITBOX_DUNEFIELD_AUDIENCE", raising=False)
    monkeypatch.delenv("RAITBOX_DUNEFIELD_TLS_SERVER_NAME", raising=False)
    with pytest.raises(InvalidArgumentException):
        Sandbox.create(template="t", backend="dunefield", gateway_url="http://127.0.0.1:8443")


def test_token_signs_audience_header():
    session = boto3.Session(
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region_name="us-east-1",
    )
    token, _expiry = iam_token_provider("cell-a.sandboxes.dunefield.internal", session=session)()
    raw = token[3:]
    url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    u = urlparse(url)
    q = parse_qs(u.query)
    assert u.hostname == "sts.us-east-1.amazonaws.com"
    assert q["Action"] == ["GetCallerIdentity"]
    assert "x-dunefield-audience" in q["X-Amz-SignedHeaders"][0].split(";")
    assert int(q["X-Amz-Expires"][0]) <= 900


def test_get_host_and_preview_link_use_router_port_route():
    router = FakeRouter()
    with Sandbox.create(
        template="python-agent:v1", backend="dunefield", client=router, timeout=600
    ) as sbx:
        assert sbx.get_host(8000) == "http://127.0.0.1:8443/v1/sandboxes/sbx-test-1/ports/8000"
        link = sbx.get_preview_link(8000)
        assert (
            link.url.endswith("/ports/8000")
            and link.headers["X-aws-proxy-auth"] == "Dunefield-IAM v1.token"
        )


def test_computer_use_not_supported():
    from raitbox.core.errors import NotSupportedError

    router = FakeRouter()
    with (
        Sandbox.create(
            template="python-agent:v1", backend="dunefield", client=router, timeout=600
        ) as sbx,
        pytest.raises(NotSupportedError),
    ):
        sbx._backend.computer_use("screenshot", {})


def test_gateway_url_needs_scheme_and_host(monkeypatch):
    monkeypatch.delenv("RAITBOX_DUNEFIELD_GATEWAY_URL", raising=False)
    with pytest.raises(InvalidArgumentException):
        Sandbox.create(template="t", backend="dunefield", gateway_url="cell-a.example")


def test_shares_agent_server_implementation_with_microvm():
    from raitbox.backends._agent_server import AgentServerBackend
    from raitbox.backends.dunefield import DunefieldBackend
    from raitbox.backends.microvm import MicroVMBackend

    assert issubclass(DunefieldBackend, AgentServerBackend) and not issubclass(
        DunefieldBackend, MicroVMBackend
    )
    assert DunefieldBackend.run_command is MicroVMBackend.run_command
