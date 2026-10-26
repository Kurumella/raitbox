"""LIVE: dunefield backend against a Dunefield cell (gVisor sandboxes on EKS via the Sandbox Router).

Requires a reachable Sandbox Router and a registered template::

    export RAITBOX_IT_DUNEFIELD_GATEWAY_URL=http://127.0.0.1:18443          # tunnel, or https://<gateway>
    export RAITBOX_IT_DUNEFIELD_AUDIENCE=use1-dev-c01.sandboxes.dunefield.internal
    export RAITBOX_IT_DUNEFIELD_TEMPLATE=python-agent:v1
    export RAITBOX_IT_DUNEFIELD_TLS_SERVER_NAME=...   # optional, when tunnelling to a Gateway
    export RAITBOX_IT_DUNEFIELD_CA_BUNDLE=...         # optional, privately issued Gateway certificate
    export RAITBOX_IT_SNAPSHOT_BUCKET=...             # optional, enables pause/resume
    AWS_DEFAULT_REGION=us-east-1 pytest -m integration tests/integration/test_dunefield_live.py -v

Dunefield sandboxes have no network egress by default, so network-dependent features are skipped.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from raitbox import AsyncSandbox, CommandExitException, Sandbox
from raitbox.core.errors import NotSupportedError, SandboxNotFoundException
from raitbox.core.types import PtySize

from .conftest import env_or_skip

pytestmark = pytest.mark.integration


def _kwargs(region: str) -> dict:
    kw = {
        "backend": "dunefield",
        "gateway_url": env_or_skip("RAITBOX_IT_DUNEFIELD_GATEWAY_URL"),
        "audience": os.environ.get("RAITBOX_IT_DUNEFIELD_AUDIENCE"),
        "tls_server_name": os.environ.get("RAITBOX_IT_DUNEFIELD_TLS_SERVER_NAME"),
        "ca_bundle": os.environ.get("RAITBOX_IT_DUNEFIELD_CA_BUNDLE"),
        "region_name": region,
    }
    return {k: v for k, v in kw.items() if v}


@pytest.fixture(scope="module")
def dkw(region):
    return _kwargs(region)


@pytest.fixture(scope="module")
def template():
    return env_or_skip("RAITBOX_IT_DUNEFIELD_TEMPLATE")


@pytest.fixture(scope="module")
def sandbox(dkw, template):
    sbx = Sandbox.create(
        template=template,
        timeout=1200,
        envs={"IT_ENV": "from-create"},
        metadata={"suite": "it"},
        **dkw,
    )
    try:
        yield sbx
    finally:
        sbx.kill()


# -- execution -----------------------------------------------------------------


def test_runs_under_gvisor(sandbox):
    assert "gvisor" in sandbox.commands.run("uname -r").stdout


def test_run_code_is_stateful(sandbox):
    assert sandbox.run_code("x = 21").error is None
    assert sandbox.run_code("print(x * 2)").logs.stdout == ["42\n"]


def test_run_code_reports_errors(sandbox):
    ex = sandbox.run_code("1 / 0")
    assert ex.error is not None and ex.error.name == "ZeroDivisionError"


def test_command_exit_code_raises(sandbox):
    with pytest.raises(CommandExitException) as err:
        sandbox.commands.run("sh -c 'echo out; echo err >&2; exit 7'")
    assert err.value.exit_code == 7 and "out" in err.value.stdout and "err" in err.value.stderr


def test_envs_and_cwd(sandbox):
    assert sandbox.commands.run("echo $IT_ENV").stdout.strip() == "from-create"
    assert sandbox.commands.run("echo $A", envs={"A": "per-call"}).stdout.strip() == "per-call"
    sandbox.files.make_dir("/workspace/sub")
    assert sandbox.commands.run("pwd", cwd="/workspace/sub").stdout.strip() == "/workspace/sub"


def test_streaming_callbacks(sandbox):
    chunks = []
    sandbox.commands.run("echo streamed", on_stdout=chunks.append)
    assert "".join(chunks).strip() == "streamed"


def test_background_command(sandbox):
    handle = sandbox.commands.run("for i in 1 2 3; do echo n$i; sleep 1; done", background=True)
    assert any(p.pid == handle.pid for p in sandbox.commands.list())
    result = handle.wait()
    assert result.exit_code == 0 and result.stdout.splitlines() == ["n1", "n2", "n3"]


def test_background_stdin_and_kill(sandbox):
    handle = sandbox.commands.run("cat", background=True)
    handle.send_stdin("echoed\n")
    time.sleep(1)
    assert handle.kill() is True


def test_pty(sandbox):
    term = sandbox.pty.create(PtySize(rows=24, cols=80))
    term.send_stdin(b"echo pty-ok; exit\n")
    term.wait()
    assert b"pty-ok" in term.data


def test_code_contexts(sandbox):
    ctx = sandbox.create_code_context()
    assert sandbox.run_code("print('ctx')", context=ctx).logs.stdout == ["ctx\n"]


# -- filesystem ------------------------------------------------------------------


def test_files_round_trip(sandbox):
    sandbox.files.write("/workspace/it/a.txt", "hello")
    sandbox.files.write("/workspace/it/b.bin", bytes(range(256)))
    assert sandbox.files.read("/workspace/it/a.txt") == "hello"
    assert bytes(sandbox.files.read("/workspace/it/b.bin", format="bytes")) == bytes(range(256))
    names = {e.name for e in sandbox.files.list("/workspace/it")}
    assert {"a.txt", "b.bin"} <= names
    assert sandbox.files.exists("/workspace/it/a.txt")
    assert sandbox.files.get_info("/workspace/it/a.txt").type == "file"
    sandbox.files.rename("/workspace/it/a.txt", "/workspace/it/c.txt")
    assert not sandbox.files.exists("/workspace/it/a.txt") and sandbox.files.exists(
        "/workspace/it/c.txt"
    )
    sandbox.files.remove("/workspace/it/c.txt")
    assert not sandbox.files.exists("/workspace/it/c.txt")


def test_write_files_batch(sandbox):
    sandbox.files.write_files(
        [
            {"path": "/workspace/batch/1.txt", "data": "one"},
            {"path": "/workspace/batch/2.txt", "data": "two"},
        ]
    )
    assert (
        sandbox.commands.run("cat /workspace/batch/1.txt /workspace/batch/2.txt").stdout == "onetwo"
    )


def test_watch_dir(sandbox):
    sandbox.files.make_dir("/workspace/watched")
    watch = sandbox.files.watch_dir("/workspace/watched")
    try:
        sandbox.files.write("/workspace/watched/new.txt", "x")
        events = []
        for _ in range(10):
            events += watch.get_new_events()
            if events:
                break
            time.sleep(1)
        assert any(e.name == "new.txt" for e in events)
    finally:
        watch.stop()


def test_git_local_repository(sandbox):
    sandbox.commands.run(
        "git init -q /workspace/repo && git -C /workspace/repo config user.email it@example.com "
        "&& git -C /workspace/repo config user.name it"
    )
    sandbox.files.write("/workspace/repo/readme.md", "# it")
    sandbox.git.add(".", cwd="/workspace/repo")
    sandbox.git.commit("initial", cwd="/workspace/repo")
    assert "initial" in sandbox.commands.run("git -C /workspace/repo log --oneline").stdout


def test_git_clone_needs_network():
    pytest.skip("Dunefield sandboxes have no egress by default (network profile 'none')")


def test_lsp(sandbox):
    if sandbox.commands.run("command -v pylsp || true").stdout.strip() == "":
        pytest.skip(
            "template image has no language server (pylsp); add it to the template to test LSP"
        )
    sandbox.files.write("/workspace/lsp/m.py", "def hello():\n    return 1\n")
    with sandbox.lsp.create_server("python", project_root="/workspace/lsp") as srv:
        assert any(s.name == "hello" for s in srv.document_symbols("/workspace/lsp/m.py"))


def test_computer_use_needs_desktop_template():
    pytest.skip(
        "computer use needs a desktop template image (Xvfb + xdotool), not part of python-agent"
    )


# -- lifecycle -------------------------------------------------------------------


def test_info_list_connect_timeout(sandbox, dkw):
    info = sandbox.get_info()
    assert info.sandbox_id == sandbox.sandbox_id and info.state == "READY"
    assert sandbox.is_running()
    assert any(
        i.sandbox_id == sandbox.sandbox_id
        for i in Sandbox.list(query={"metadata": {"suite": "it"}}, **dkw)
    )
    sandbox.set_timeout(1500)
    again = Sandbox.connect(sandbox.sandbox_id, **dkw)
    assert again.commands.run("echo connected").stdout.strip() == "connected"


def test_unsupported_features_fail_loudly(sandbox):
    with pytest.raises(NotSupportedError):
        sandbox.get_host(8080)
    with pytest.raises(NotSupportedError):
        sandbox.get_preview_link(8080)


def test_context_manager_deletes(dkw, template):
    with Sandbox.create(template=template, timeout=600, **dkw) as sbx:
        sid = sbx.sandbox_id
    with pytest.raises(SandboxNotFoundException):
        Sandbox.connect(sid, **dkw)


def test_pause_and_resume(dkw, template):
    bucket = os.environ.get("RAITBOX_IT_SNAPSHOT_BUCKET")
    if not bucket:
        pytest.skip("set RAITBOX_IT_SNAPSHOT_BUCKET to test pause/resume")
    sbx = Sandbox.create(template=template, timeout=600, **dkw)
    sbx.files.write("/workspace/keep/state.txt", "persisted")
    snap = sbx.pause(bucket=bucket)
    restored = Sandbox.resume(snap, bucket=bucket, template=template, timeout=600, **dkw)
    try:
        assert restored.files.read("/workspace/keep/state.txt") == "persisted"
    finally:
        restored.kill()


def test_async_sandbox(dkw, template):
    async def run():
        async with await AsyncSandbox.create(template=template, timeout=600, **dkw) as sbx:
            await sbx.files.write("/workspace/async.txt", "async-ok")
            out = (await sbx.commands.run("cat /workspace/async.txt")).stdout
            code = (await sbx.run_code("print(1 + 1)")).logs.stdout
            return out, code

    out, code = asyncio.run(run())
    assert out == "async-ok" and code == ["2\n"]


def test_concurrent_sandboxes(dkw, template):
    async def one(i):
        async with await AsyncSandbox.create(template=template, timeout=600, **dkw) as sbx:
            return (await sbx.run_code(f"print({i} * {i})")).logs.stdout

    async def many():
        return await asyncio.gather(*(one(i) for i in range(4)))

    assert asyncio.run(many()) == [[f"{i * i}\n"] for i in range(4)]


def test_warm_pool(dkw, template):
    from raitbox.core.warm_pool import WarmPool

    with WarmPool(size=2, template=template, timeout=600, **dkw) as pool:
        deadline = time.time() + 120
        while pool.available < 2 and time.time() < deadline:
            time.sleep(1)
        assert pool.available == 2
        sbx = pool.acquire()
        try:
            assert sbx.commands.run("echo pooled").stdout.strip() == "pooled"
        finally:
            sbx.kill()
