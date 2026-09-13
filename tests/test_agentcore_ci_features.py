"""Newly wired agentcore-ci capabilities: rename/make_dir, list, secrets→commands."""

from __future__ import annotations

from raitbox import Sandbox
from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend as CI
from tests.conftest import FakeCIClient


def _sbx(client, **kw):
    return Sandbox(backend="agentcore-ci", client=client, **kw)


def test_files_make_dir_and_rename_shell_out():
    client = FakeCIClient()
    sbx = _sbx(client)
    assert sbx.files.make_dir("/work/sub") is True
    info = sbx.files.rename("/work/a.txt", "/work/b.txt")
    assert info.path == "/work/b.txt"
    # Both went through executeCommand as mkdir/mv (quoted).
    assert any(c.startswith("mkdir -p ") and "/work/sub" in c for c in client.cmd_args)
    assert any(
        c.startswith("mv ") and "/work/a.txt" in c and "/work/b.txt" in c for c in client.cmd_args
    )


def test_secrets_reach_shell_commands():
    client = FakeCIClient()
    sbx = _sbx(client, secrets={"API_KEY": "xyz"})
    # Clear the seed-env executeCode noise; run a shell command.
    client.cmd_args.clear()
    sbx.commands.run("printenv API_KEY")
    assert client.cmd_args, "no command was issued"
    assert "API_KEY=xyz" in client.cmd_args[-1]  # secret prefixed onto the shell cmd


def test_per_call_env_overrides_seeded():
    client = FakeCIClient()
    sbx = _sbx(client, secrets={"API_KEY": "seed"})
    client.cmd_args.clear()
    sbx.commands.run("printenv API_KEY", envs={"API_KEY": "override"})
    assert "API_KEY=override" in client.cmd_args[-1]
    assert "API_KEY=seed" not in client.cmd_args[-1]


def test_sandbox_list_uses_code_interpreter_sessions():
    client = FakeCIClient()
    # Start two sessions; kill one so only the live one is listed.
    a = _sbx(client)
    _sbx(client)
    a.kill()
    infos = Sandbox.list(backend="agentcore-ci", client=client)
    ids = [i.sandbox_id for i in infos]
    assert a.sandbox_id not in ids  # killed → not READY
    assert len(ids) == 1 and all(i.state == "READY" for i in infos)


def test_backend_list_running_paginates_and_maps():
    client = FakeCIClient()
    _sbx(client)
    infos = CI.list_running(client=client)
    assert infos and infos[0].template == "aws.codeinterpreter.v1"
    assert infos[0].state == "READY"
