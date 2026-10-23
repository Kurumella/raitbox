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
            return FakeResp({"sandbox_id": "sbx-test-1", "state": "READY", "template": json["template"]})
        if path == "/run_code":
            return FakeResp({"stdout": "42\n", "stderr": "", "results": []})
        if path == "/run_command":
            return FakeResp({"stdout": "hi\n", "stderr": "", "exit_code": 0})
        return FakeResp({"ok": True})

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)


def test_lifecycle_through_router():
    router = FakeRouter()
    with Sandbox.create(template="python-agent:v1", backend="dunefield", client=router, timeout=600) as sbx:
        assert sbx.sandbox_id == "sbx-test-1"
        assert sbx.run_code("print(6*7)").logs.stdout == ["42\n"]
        assert sbx.commands.run("echo hi").stdout == "hi\n"
    methods = [(m, p, s) for m, p, s, _ in router.calls]
    assert ("POST", "/v1/sandboxes", None) in methods
    assert ("POST", "/run_code", "sbx-test-1") in methods            # data calls carry the sandbox id
    assert ("DELETE", "/v1/sandboxes/sbx-test-1", None) in methods   # context exit deletes the sandbox


def test_aliases_resolve_to_dunefield():
    from raitbox.core.settings import resolve_backend
    assert {resolve_backend(n) for n in ("dunefield", "eks", "k8s", "gvisor")} == {"dunefield"}


def test_tunnel_requires_audience(monkeypatch):
    monkeypatch.delenv("RAITBOX_DUNEFIELD_AUDIENCE", raising=False)
    monkeypatch.delenv("RAITBOX_DUNEFIELD_TLS_SERVER_NAME", raising=False)
    with pytest.raises(InvalidArgumentException):
        Sandbox.create(template="t", backend="dunefield", gateway_url="http://127.0.0.1:8443")


def test_token_signs_audience_header():
    session = boto3.Session(aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region_name="us-east-1")
    token, expiry = iam_token_provider("cell-a.sandboxes.dunefield.internal", session=session)()
    raw = token[3:]
    url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    u = urlparse(url)
    q = parse_qs(u.query)
    assert u.hostname == "sts.us-east-1.amazonaws.com"
    assert q["Action"] == ["GetCallerIdentity"]
    assert "x-dunefield-audience" in q["X-Amz-SignedHeaders"][0].split(";")
    assert int(q["X-Amz-Expires"][0]) <= 900
