"""LIVE: agentcore-ci backend against Bedrock AgentCore Code Interpreter.

Lowest-setup backend — needs only AWS credentials + a region where AgentCore
Code Interpreter is available. Run with::

    AWS_PROFILE=... AWS_DEFAULT_REGION=us-west-2 pytest -m integration \\
        tests/integration/test_agentcore_ci_live.py -v
"""

from __future__ import annotations

import pytest

from raitbox import CommandExitException, Sandbox

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(region):
    sbx = Sandbox.create(backend="agentcore-ci", region_name=region, timeout=120)
    try:
        yield sbx
    finally:
        sbx.kill()


def test_run_code_roundtrip(sandbox):
    execution = sandbox.run_code("print('hello from sandbox')")
    assert execution.error is None
    assert "hello from sandbox" in "".join(execution.logs.stdout)


def test_stateful_kernel(sandbox):
    sandbox.run_code("_x = 20 + 22")
    out = sandbox.run_code("print(_x)")
    assert "42" in (out.text or "") or "42" in "".join(out.logs.stdout)


def test_files_roundtrip(sandbox):
    sandbox.files.write("it/data.txt", "payload")
    assert sandbox.files.read("it/data.txt") == "payload"
    assert sandbox.files.exists("it/data.txt")


def test_command_nonzero_raises(sandbox):
    with pytest.raises(CommandExitException):
        sandbox.commands.run("exit 7")
