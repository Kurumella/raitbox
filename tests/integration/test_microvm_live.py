"""LIVE: microvm backend against AWS Lambda MicroVMs.

Requires a prebuilt RaitBox image (build with ``raitbox.tooling.build_image``)::

    export RAITBOX_IT_MICROVM_IMAGE=arn:aws:lambda:REGION:ACCT:microvm-image:raitbox-sandbox
    export RAITBOX_IT_EXECUTION_ROLE=arn:aws:iam::ACCT:role/raitbox-exec   # optional
    AWS_DEFAULT_REGION=us-east-1 pytest -m integration \\
        tests/integration/test_microvm_live.py -v
"""

from __future__ import annotations

import os

import pytest

from raitbox import Sandbox

from .conftest import env_or_skip

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(region):
    image = env_or_skip("RAITBOX_IT_MICROVM_IMAGE")
    kwargs = {"backend": "microvm", "template": image, "region_name": region, "timeout": 600}
    if os.environ.get("RAITBOX_IT_EXECUTION_ROLE"):
        kwargs["execution_role_arn"] = os.environ["RAITBOX_IT_EXECUTION_ROLE"]
    if os.environ.get("RAITBOX_IT_EGRESS"):
        kwargs["egress_connectors"] = [os.environ["RAITBOX_IT_EGRESS"]]
    sbx = Sandbox.create(**kwargs)
    try:
        yield sbx
    finally:
        sbx.kill()


def test_run_command(sandbox):
    result = sandbox.commands.run("echo hello && uname -s")
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_run_code_and_files(sandbox):
    sandbox.files.write("/tmp/it/app.py", "print(6 * 7)")
    out = sandbox.commands.run("python3 /tmp/it/app.py").stdout
    assert "42" in out
    assert sandbox.files.read("/tmp/it/app.py") == "print(6 * 7)"


def test_background_command(sandbox):
    handle = sandbox.commands.run("for i in 1 2 3; do echo n$i; sleep 1; done", background=True)
    result = handle.wait()
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["n1", "n2", "n3"]


def test_get_host_returns_endpoint(sandbox):
    host = sandbox.get_host(8080)
    assert host and "on.aws" in host


def test_list_running_includes_sandbox(sandbox, region):
    ids = {info.sandbox_id for info in Sandbox.list(backend="microvm", region_name=region)}
    assert sandbox.sandbox_id in ids
