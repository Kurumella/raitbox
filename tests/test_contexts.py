"""run_code contexts (agentcore-ci maps them to clearContext)."""

from __future__ import annotations

import pytest

from raitbox import InvalidArgumentException, Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from tests.conftest import FakeCIClient


def _sandbox():
    client = FakeCIClient()
    backend = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client,
    )
    return Sandbox(_backend=backend), client


def test_first_run_in_context_clears_kernel():
    sbx, client = _sandbox()
    ctx = sbx.create_code_context()
    sbx.run_code("x = 1", context=ctx)
    sbx.run_code("print(x)", context=ctx)
    # First execution in the context resets the kernel; the second does not.
    assert client.code_args[0].get("clearContext") is True
    assert "clearContext" not in client.code_args[1]


def test_context_and_language_are_mutually_exclusive():
    sbx, _ = _sandbox()
    ctx = sbx.create_code_context()
    with pytest.raises(InvalidArgumentException):
        sbx.run_code("1", language="python", context=ctx)


def test_context_cwd_prepended():
    sbx, client = _sandbox()
    ctx = sbx.create_code_context(cwd="/work")
    sbx.run_code("pass", context=ctx)
    assert "os.chdir('/work')" in client.code_args[0]["code"]
