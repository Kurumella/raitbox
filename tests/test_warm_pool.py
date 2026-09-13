"""Warm pool: pre-warming, instant acquire, replenish, and teardown."""

from __future__ import annotations

import threading
import time

import pytest

from raitbox import WarmPool
from raitbox.core import warm_pool as wp_mod


class FakeSandbox:
    _counter = 0
    _lock = threading.Lock()

    def __init__(self) -> None:
        with FakeSandbox._lock:
            FakeSandbox._counter += 1
            self.id = FakeSandbox._counter
        self.killed = False

    def kill(self) -> bool:
        self.killed = True
        return True


@pytest.fixture(autouse=True)
def _fake_create(monkeypatch):
    FakeSandbox._counter = 0

    class _S:
        @staticmethod
        def create(**kwargs):
            return FakeSandbox()

    monkeypatch.setattr(wp_mod, "Sandbox", _S, raising=False)
    # WarmPool imports Sandbox lazily inside _create; patch that import target.
    monkeypatch.setitem(
        __import__("sys").modules,
        "raitbox.client.sync",
        type("M", (), {"Sandbox": _S}),
    )


def _wait_until(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_pool_prewarms_to_size():
    pool = WarmPool(size=3)
    assert _wait_until(lambda: pool.available == 3)
    pool.close()


def test_acquire_returns_ready_and_replenishes():
    pool = WarmPool(size=2)
    assert _wait_until(lambda: pool.available == 2)
    sbx = pool.acquire(timeout=1)
    assert isinstance(sbx, FakeSandbox)
    # Pool refills back to size after the handout.
    assert _wait_until(lambda: pool.available == 2)
    pool.close()


def test_acquire_when_empty_creates_inline():
    pool = WarmPool(size=1, warm=False)
    assert pool.available == 0
    sbx = pool.acquire()  # no warm sandbox ready → created synchronously
    assert isinstance(sbx, FakeSandbox)
    pool.close()


def test_close_kills_idle_and_blocks_acquire():
    pool = WarmPool(size=2)
    assert _wait_until(lambda: pool.available == 2)
    idle = list(pool._ready.queue)
    pool.close()
    assert all(s.killed for s in idle)
    from raitbox import SandboxException

    with pytest.raises(SandboxException):
        pool.acquire()


def test_invalid_size():
    with pytest.raises(ValueError, match="size must be"):
        WarmPool(size=0)
