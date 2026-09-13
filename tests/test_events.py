"""EventBridge webhooks: lifecycle events emitted when configured, off by default."""

from __future__ import annotations

import json
from typing import Any

import pytest

from raitbox import Sandbox
from raitbox.core import events as events_mod
from tests.conftest import FakeCIClient


class FakeEvents:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


@pytest.fixture(autouse=True)
def _reset_events():
    events_mod.reset()
    yield
    events_mod.reset()


def test_disabled_by_default_emits_nothing():
    fake = FakeEvents()
    # Even with a client sitting in state, emission is off until configured.
    events_mod._STATE["client"] = fake
    events_mod.emit("sandbox.created", {"sandbox_id": "x"})
    assert fake.entries == []


def test_configure_and_emit():
    fake = FakeEvents()
    events_mod.configure(client=fake, bus_name="my-bus", source="agent")
    events_mod.emit("sandbox.created", {"sandbox_id": "s1", "backend": "ci"})
    (entry,) = fake.entries
    assert entry["Source"] == "agent"
    assert entry["DetailType"] == "sandbox.created"
    assert entry["EventBusName"] == "my-bus"
    assert json.loads(entry["Detail"]) == {"sandbox_id": "s1", "backend": "ci"}


def test_emit_swallows_client_errors():
    class Boom:
        def put_events(self, Entries):
            raise RuntimeError("bus down")

    events_mod.configure(client=Boom(), bus_name="b")
    events_mod.emit("sandbox.created", {"sandbox_id": "s"})  # must not raise


def test_sandbox_lifecycle_emits_created_and_killed():
    fake = FakeEvents()
    events_mod.configure(client=fake, bus_name="b")
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    sbx.kill()
    types = [e["DetailType"] for e in fake.entries]
    assert types == ["sandbox.created", "sandbox.killed"]
    assert all(json.loads(e["Detail"])["backend"] == "agentcore-ci" for e in fake.entries)


def test_env_autoconfigure(monkeypatch):
    monkeypatch.setenv("RAITBOX_EVENT_BUS", "env-bus")
    fake = FakeEvents()
    events_mod._STATE["client"] = fake  # avoid real boto3
    events_mod.emit("sandbox.created", {"sandbox_id": "s"})
    assert fake.entries and fake.entries[0]["EventBusName"] == "env-bus"
