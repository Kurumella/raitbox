"""Pause/resume S3 file snapshot (fake S3, CI backends)."""

from __future__ import annotations

import io

import pytest

from raitbox.backends.agentcore_ci import AgentCoreCodeInterpreterBackend
from raitbox.core import persistence as snapshot_mod
from raitbox.core.errors import InvalidArgumentException
from tests.conftest import FakeCIClient


class _Body:
    def __init__(self, data: bytes) -> None:
        self._b = io.BytesIO(data)

    def read(self) -> bytes:
        return self._b.read()


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict]] = {}

    def put_object(self, Bucket, Key, Body, Metadata=None, **kw):
        self.objects[Key] = (Body, Metadata or {})

    def get_object(self, Bucket, Key, **kw):
        data, meta = self.objects[Key]
        return {"Body": _Body(data), "Metadata": meta}

    def get_paginator(self, _name):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix, **kw):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}

        return _P()


def _backend(client=None):
    return AgentCoreCodeInterpreterBackend.open(
        template=None,
        timeout=300,
        envs=None,
        metadata=None,
        region_name=None,
        profile_name=None,
        client=client or FakeCIClient(),
    )


def test_save_and_load_snapshot(monkeypatch):
    fake_s3 = FakeS3()
    monkeypatch.setattr(snapshot_mod.boto3, "client", lambda svc: fake_s3)

    src = _backend()
    src.file_write("app/main.py", "print('hi')")
    src.file_write("data.txt", "payload")
    sandbox_id = snapshot_mod.save_snapshot(src, bucket="my-bucket")
    assert len(fake_s3.objects) == 2

    dst = _backend()
    snapshot_mod.load_snapshot(dst, sandbox_id, bucket="my-bucket")
    assert dst.file_read("app/main.py") == "print('hi')"
    assert dst.file_read("data.txt") == "payload"


def test_snapshot_requires_bucket(monkeypatch):
    monkeypatch.delenv("RAITBOX_SNAPSHOT_BUCKET", raising=False)
    with pytest.raises(InvalidArgumentException):
        snapshot_mod.save_snapshot(_backend(), bucket=None)


def test_snapshot_bucket_from_env(monkeypatch):
    fake_s3 = FakeS3()
    monkeypatch.setattr(snapshot_mod.boto3, "client", lambda svc: fake_s3)
    monkeypatch.setenv("RAITBOX_SNAPSHOT_BUCKET", "env-bucket")
    src = _backend()
    src.file_write("f.txt", "x")
    snapshot_mod.save_snapshot(src)
    assert any(k.startswith("raitbox-snapshots/") for k in fake_s3.objects)
