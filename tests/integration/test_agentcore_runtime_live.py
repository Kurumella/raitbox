"""LIVE: agentcore-runtime backend against a deployed AgentCore Runtime.

Requires an already-deployed runtime (RaitBox does not provision runtimes)::

    export RAITBOX_IT_RUNTIME_ARN=arn:aws:bedrock-agentcore:REGION:ACCT:runtime/NAME
    AWS_DEFAULT_REGION=us-west-2 pytest -m integration \\
        tests/integration/test_agentcore_runtime_live.py -v
"""

from __future__ import annotations

import pytest

from raitbox import Sandbox

from .conftest import env_or_skip

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(region):
    arn = env_or_skip("RAITBOX_IT_RUNTIME_ARN")
    sbx = Sandbox.create(backend="agentcore-runtime", template=arn, region_name=region, timeout=300)
    try:
        yield sbx
    finally:
        sbx.kill()


def test_run_command_streams(sandbox):
    chunks: list[str] = []
    result = sandbox.commands.run("echo hello-runtime", on_stdout=chunks.append)
    assert result.exit_code == 0
    assert "hello-runtime" in result.stdout
    assert "hello-runtime" in "".join(chunks)


def test_files_over_shell(sandbox):
    sandbox.files.write("/tmp/it_runtime.txt", "runtime-payload")
    assert sandbox.files.read("/tmp/it_runtime.txt") == "runtime-payload"


def test_interactive_shell(sandbox):
    """Only runs if the bedrock-agentcore SDK is installed (the 'runtime' extra)."""
    pytest.importorskip("bedrock_agentcore")
    import asyncio

    async def _drive() -> str:
        collected = []
        async with sandbox.open_shell() as shell:
            await shell.send("echo shell-works\n")
            async for frame in shell:
                collected.append(getattr(frame, "text", ""))
                if "shell-works" in "".join(collected):
                    break
        return "".join(collected)

    assert "shell-works" in asyncio.run(_drive())
