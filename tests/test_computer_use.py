"""Computer use: grouped namespaces dispatch to the backend; CI is unsupported."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from raitbox import NotSupportedError, Sandbox
from raitbox.backends import microvm as microvm_mod
from tests.conftest import FakeCIClient

IMAGE = "arn:aws:lambda:us-east-1:123456789012:microvm-image:raitbox"


class FakeComputerBackend:
    name = "microvm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def computer_use(self, action: str, params: dict[str, Any]) -> Any:
        self.calls.append((action, params))
        if action == "screenshot":
            return {"png": base64.b64encode(b"PNGDATA").decode()}
        if action == "mouse.position":
            return {"x": 3, "y": 4}
        return {"ok": True}


def _cu():
    from raitbox.client.sync import ComputerUse

    b = FakeComputerBackend()
    return ComputerUse(b), b


def test_mouse_actions_dispatch():
    cu, b = _cu()
    cu.mouse.move(10, 20)
    cu.mouse.click(10, 20, button="right", double=True)
    cu.mouse.scroll("down", 3)
    assert cu.mouse.get_position() == {"x": 3, "y": 4}
    assert b.calls[0] == ("mouse.move", {"x": 10, "y": 20})
    assert b.calls[1] == ("mouse.click", {"button": 3, "double": True, "x": 10, "y": 20})
    assert b.calls[2] == ("mouse.scroll", {"direction": "down", "amount": 3})


def test_keyboard_and_screenshot():
    cu, b = _cu()
    cu.keyboard.type("hello")
    cu.keyboard.hotkey("ctrl", "c")
    png = cu.screenshot.take()
    assert png == b"PNGDATA"
    assert ("keyboard.type", {"text": "hello"}) in b.calls
    assert ("keyboard.press", {"key": "c", "modifiers": ["ctrl"]}) in b.calls


def test_lifecycle_and_display():
    cu, b = _cu()
    cu.start()
    cu.get_status()
    cu.display.get_info()
    cu.stop()
    assert [a for a, _ in b.calls] == ["start", "status", "display.info", "stop"]


def test_computer_use_not_supported_on_ci():
    sbx = Sandbox(backend="agentcore-ci", client=FakeCIClient())
    with pytest.raises(NotSupportedError):
        sbx.computer_use.screenshot.take()


def test_microvm_computer_use_over_http():
    class FakeHttp:
        def post(self, path: str, json: dict | None = None, **kw: Any):
            assert path == "/computer"
            return type("R", (), {"json": lambda self: {"result": {"ok": True}}})()

        def get(self, *a: Any, **k: Any):
            return None

    b = microvm_mod.MicroVMBackend(
        client=object(),
        microvm_id="mv-1",
        endpoint="mv-1.on.aws",
        template=IMAGE,
        http=FakeHttp(),
        timeout=300,
    )
    assert b.computer_use("mouse.move", {"x": 1, "y": 2}) == {"ok": True}


def test_agent_server_computer_dispatch_unknown_action():
    pytest.importorskip("fastapi")
    from raitbox.agent_server import server as srv_mod

    with pytest.raises(ValueError, match="unknown computer-use action"):
        srv_mod._ComputerUse().dispatch("bogus.op", {})
