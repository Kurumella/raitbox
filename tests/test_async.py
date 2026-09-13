"""AsyncSandbox tests (thread-offloading facade over the CI backend)."""

from __future__ import annotations

import asyncio

from raitbox import AsyncSandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from raitbox.client.sync import Sandbox
from tests.conftest import FakeCIClient


def _async_sandbox() -> AsyncSandbox:
    backend = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=FakeCIClient(),
    )
    return AsyncSandbox(Sandbox(_backend=backend))


async def test_async_run_code():
    sbx = _async_sandbox()
    execution = await sbx.run_code("print('hello from sandbox')")
    assert execution.logs.stdout == ["hello from sandbox\n"]
    await sbx.kill()


async def test_async_files_roundtrip():
    sbx = _async_sandbox()
    await sbx.files.write("f.txt", "data")
    assert await sbx.files.read("f.txt") == "data"
    assert await sbx.files.exists("f.txt")
    await sbx.kill()


async def test_async_context_manager():
    backend = AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=FakeCIClient(),
    )
    async with AsyncSandbox(Sandbox(_backend=backend)) as sbx:
        assert (await sbx.run_code("print('x')")).error is None


async def test_async_concurrent_sandboxes():
    boxes = [_async_sandbox() for _ in range(3)]
    results = await asyncio.gather(*(b.run_code("print('hello from sandbox')") for b in boxes))
    assert all(r.text == "hello from sandbox" for r in results)
    await asyncio.gather(*(b.kill() for b in boxes))
