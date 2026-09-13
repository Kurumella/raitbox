"""E2B drop-in compatibility surface: static/instance methods, callbacks,
write overload, watch_dir, list. Uses the CI fake and the e2e MicroVM server.
"""

from __future__ import annotations

import time

import pytest

from raitbox import Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from tests.conftest import FakeCIClient


def _ci_sandbox(client=None):
    client = client or FakeCIClient()
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


# -- run_code E2B signature + callbacks -----------------------------------


def test_run_code_fires_result_and_stdout_callbacks():
    sbx, _ = _ci_sandbox()
    results, out = [], []
    ex = sbx.run_code("print('hello from sandbox')", on_result=results.append, on_stdout=out.append)
    assert ex.text == "hello from sandbox"
    assert results and results[0].text == "hello from sandbox"
    assert out == ["hello from sandbox\n"]


def test_run_code_fires_on_error():
    sbx, _ = _ci_sandbox()
    errors = []
    sbx.run_code("1/0", on_error=errors.append)
    assert errors and errors[0].name == "ZeroDivisionError"


def test_run_code_accepts_e2b_kwargs():
    sbx, _ = _ci_sandbox()
    # These E2B params must be accepted even where they're best-effort.
    ex = sbx.run_code("print('hello from sandbox')", envs={"A": "b"}, timeout=30, request_timeout=5)
    assert ex.error is None


# -- files.write overload + write_files -----------------------------------


def test_files_write_overload_list():
    sbx, _ = _ci_sandbox()
    infos = sbx.files.write(
        [
            {"path": "a.txt", "data": "1"},
            {"path": "b.txt", "data": "2"},
        ]
    )
    assert isinstance(infos, list) and {i.path for i in infos} == {"a.txt", "b.txt"}
    assert sbx.files.read("a.txt") == "1"


# -- static vs instance lifecycle -----------------------------------------


def test_static_kill_uses_connect(monkeypatch):
    # Sandbox.kill("id") should connect+kill. Verify it routes through connect.
    calls = {}

    def fake_connect(sandbox_id, **kw):
        calls["id"] = sandbox_id
        sbx, _ = _ci_sandbox()
        return sbx

    monkeypatch.setattr(Sandbox, "connect", staticmethod(fake_connect))
    assert Sandbox.kill("sbx-123") is True
    assert calls["id"] == "sbx-123"


def test_instance_kill_still_works():
    sbx, client = _ci_sandbox()
    assert sbx.kill() is True
    assert client.stopped == [sbx.sandbox_id]


def test_static_get_info(monkeypatch):
    def fake_connect(sandbox_id, **kw):
        sbx, _ = _ci_sandbox()
        return sbx

    monkeypatch.setattr(Sandbox, "connect", staticmethod(fake_connect))
    info = Sandbox.get_info("sbx-xyz")
    assert info.sandbox_id  # came from the connected sandbox


# -- watch_dir (generic polling) ------------------------------------------


def test_watch_dir_detects_create_and_remove():
    sbx, _ = _ci_sandbox()
    sbx.files.write("watched/a.txt", "x")
    handle = sbx.files.watch_dir(
        "watched",
    )
    try:
        sbx.files.write("watched/b.txt", "y")
        # poll for the create event (watcher runs on a 1s interval)
        events = []
        for _ in range(30):
            events += handle.get_new_events()
            if any(e.name == "b.txt" and e.type == "create" for e in events):
                break
            time.sleep(0.1)
        assert any(e.name == "b.txt" and e.type == "create" for e in events)
    finally:
        handle.stop()


# -- Sandbox.list on the CI backend (lists Code Interpreter sessions) -----


def test_list_supported_on_ci():
    from tests.conftest import FakeCIClient

    client = FakeCIClient()
    Sandbox(backend="agentcore-ci", client=client)  # creates one live session
    infos = Sandbox.list(backend="agentcore-ci", client=client)
    assert [i.sandbox_id for i in infos] == ["ci-0"]


def test_list_not_supported_on_runtime():
    from raitbox import NotSupportedError

    with pytest.raises(NotSupportedError):
        Sandbox.list(backend="agentcore-runtime")


# -- richer result models -------------------------------------------------


def test_execution_and_result_to_json_and_formats():
    from raitbox import Execution, ExecutionError, Logs, Result

    ex = Execution(results=[Result(text="hi", html="<b>hi</b>")], logs=Logs(stdout=["hi\n"]))
    assert "hi" in ex.to_json()
    assert ex.results[0].formats() == ["text", "html"]
    assert ex.results[0]._repr_html_() == "<b>hi</b>"
    assert '"stdout"' in Logs(stdout=["x"]).to_json()
    assert '"name"' in ExecutionError("E", "v", "tb").to_json()


# -- code contexts (create/list/restart/remove) ---------------------------


def test_code_context_lifecycle():
    sbx, client = _ci_sandbox()
    ctx = sbx.create_code_context(cwd="/tmp")
    assert ctx in sbx.list_code_contexts()
    sbx.run_code("v = 1", context=ctx)  # consumes the initial clearContext
    sbx.restart_code_context(ctx)  # marks it fresh again
    sbx.run_code("print(v)", context=ctx)
    # two clearContext resets: initial create + restart
    clears = [a for a in client.code_args if a.get("clearContext")]
    assert len(clears) == 2
    sbx.remove_code_context(ctx)
    assert ctx not in sbx.list_code_contexts()


# -- git namespace (over the shell) ---------------------------------------


def test_git_status_runs_over_shell(monkeypatch):
    sbx, _ = _ci_sandbox()
    calls = []

    from raitbox.core.types import CommandResult

    def fake_run_command(cmd, **kw):
        calls.append(cmd)
        return CommandResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(sbx._backend, "run_command", fake_run_command)
    sbx.git.status(cwd="/repo")
    assert calls and calls[0].startswith("git status")


def test_git_error_raises_git_exception(monkeypatch):
    from raitbox import GitUpstreamException
    from raitbox.core.types import CommandResult

    sbx, _ = _ci_sandbox()
    monkeypatch.setattr(
        sbx._backend,
        "run_command",
        lambda cmd, **kw: CommandResult(stdout="", stderr="fatal: repo not found", exit_code=1),
    )
    with pytest.raises(GitUpstreamException):
        sbx.git.pull()


# -- presence of E2B methods (clear NotSupportedError, not AttributeError) -


def test_unsupported_methods_present_and_raise():
    from raitbox import NotSupportedError

    sbx, _ = _ci_sandbox()
    for call in (sbx.fork, sbx.update_network, sbx.get_mcp_token, sbx.create_snapshot):
        with pytest.raises(NotSupportedError):
            call()
    from raitbox import PtySize

    # PTY is a real feature on microvm; on agentcore-ci it raises NotSupported.
    with pytest.raises(NotSupportedError):
        sbx.pty.create(PtySize(rows=24, cols=80))


# -- paginator ------------------------------------------------------------


def test_sandbox_paginator():
    from raitbox import SandboxInfo
    from raitbox.core.handles import SandboxPaginator

    p = SandboxPaginator([SandboxInfo("a"), SandboxInfo("b")], page_size=1)
    assert p.has_next
    assert [i.sandbox_id for i in p.next_items()] == ["a"]
    assert p.has_next
    assert [i.sandbox_id for i in p.next_items()] == ["b"]
    assert not p.has_next
