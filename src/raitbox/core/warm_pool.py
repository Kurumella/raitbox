"""Warm pools — keep pre-started sandboxes ready to cut cold-start latency.

A :class:`WarmPool` maintains up to ``size`` sandboxes started ahead of time (in
background threads). :meth:`acquire` hands one back immediately when the pool is
warm — falling back to a synchronous create when it is empty — and schedules a
replenish so the pool refills itself. This is a client-side pool (no server-side
state); it works with any backend that ``Sandbox.create`` supports.

Exposed additively (no change to the E2B surface). Typical use::

    with WarmPool(size=3, backend="microvm", template=IMAGE) as pool:
        sbx = pool.acquire()      # ready instantly once warm
        ...                       # use sbx like any Sandbox; caller owns its lifecycle
"""

from __future__ import annotations

import contextlib
import queue
import threading
from typing import TYPE_CHECKING, Any

from .errors import SandboxException

if TYPE_CHECKING:
    from ..client.sync import Sandbox


class WarmPool:
    """A pool of pre-warmed sandboxes sharing one create configuration."""

    def __init__(self, size: int = 2, *, warm: bool = True, **create_kwargs: Any) -> None:
        if size < 1:
            raise ValueError("WarmPool size must be >= 1")
        self._size = size
        self._kwargs = create_kwargs
        self._ready: queue.Queue[Sandbox] = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._closed = False
        if warm:
            self.fill()

    # -- internals ---------------------------------------------------------

    def _create(self) -> Sandbox:
        from ..client.sync import Sandbox

        return Sandbox.create(**self._kwargs)

    def _spawn_one(self) -> None:
        try:
            # A failed warm spawn must not crash the pool; just drop this slot.
            with contextlib.suppress(Exception):
                sbx = self._create()
                if self._closed:
                    sbx.kill()
                else:
                    self._ready.put(sbx)
        finally:
            with self._lock:
                self._pending -= 1

    # -- public API --------------------------------------------------------

    @property
    def available(self) -> int:
        """Sandboxes ready to hand out right now."""
        return self._ready.qsize()

    def fill(self) -> None:
        """Top the pool back up to ``size`` (spawns happen in the background)."""
        if self._closed:
            return
        with self._lock:
            need = self._size - self._ready.qsize() - self._pending
            self._pending += max(need, 0)
        for _ in range(max(need, 0)):
            threading.Thread(target=self._spawn_one, daemon=True).start()

    def acquire(self, timeout: float | None = None) -> Sandbox:
        """Return a ready sandbox, then replenish. Creates one inline if empty.

        The caller owns the returned sandbox's lifecycle (``kill``/context
        manager); it is no longer tracked by the pool.
        """
        if self._closed:
            raise SandboxException("WarmPool is closed")
        try:
            sbx = (
                self._ready.get(timeout=timeout)
                if timeout is not None
                else self._ready.get_nowait()
            )
        except queue.Empty:
            sbx = self._create()
        self.fill()
        return sbx

    def close(self) -> None:
        """Kill all idle pooled sandboxes. Handed-out sandboxes are unaffected."""
        self._closed = True
        while True:
            try:
                sbx = self._ready.get_nowait()
            except queue.Empty:
                break
            with contextlib.suppress(Exception):  # best-effort teardown
                sbx.kill()

    def __enter__(self) -> WarmPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
