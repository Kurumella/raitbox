"""Secret resolution + injection into sandbox envs."""

from __future__ import annotations

from raitbox import Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend  # noqa: F401
from raitbox.core.secrets import resolve_secrets


class FakeSM:
    def get_secret_value(self, SecretId: str):
        return {"SecretString": {"db/plain": "s3cr3t", "db/json": '{"password": "pw"}'}[SecretId]}


class FakeSSM:
    def get_parameter(self, Name: str, WithDecryption: bool):
        return {"Parameter": {"Value": "param-val"}}


def _factory(svc: str):
    return {"secretsmanager": FakeSM(), "ssm": FakeSSM()}[svc]


def test_resolve_all_reference_kinds():
    out = resolve_secrets(
        {
            "PLAIN": "secretsmanager:db/plain",
            "PW": "secretsmanager:db/json::password",
            "PARAM": "ssm:/app/token",
            "LITERAL": "just-a-value",
        },
        client_factory=_factory,
    )
    assert out == {"PLAIN": "s3cr3t", "PW": "pw", "PARAM": "param-val", "LITERAL": "just-a-value"}


def test_secrets_merged_into_envs(monkeypatch):
    from raitbox.core import secrets as secrets_mod
    from tests.conftest import FakeCIClient

    monkeypatch.setattr(secrets_mod, "resolve_secrets", lambda *a, **k: {"API_KEY": "xyz"})
    client = FakeCIClient()
    Sandbox(backend="agentcore-ci", secrets={"API_KEY": "secretsmanager:whatever"}, client=client)
    # the CI backend seeds env via an executeCode call; the secret must appear there
    assert any(
        "API_KEY" in a.get("code", "") and "xyz" in a.get("code", "") for a in client.code_args
    )
