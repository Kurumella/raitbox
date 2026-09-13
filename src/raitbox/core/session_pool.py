"""Live-session registry with an atexit reclamation hook.

Cost guardrail: every backend session bills until it is stopped. If user code
forgets ``sandbox.kill()``, the atexit hook stops all still-registered sessions
and emits a ``ResourceWarning``. ``RAITBOX_MAX_SESSIONS`` caps concurrency.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
import warnings
from collections.abc import Callable

from .settings import max_concurrent

_lock = threading.Lock()
# sandbox_id -> zero-arg best-effort killer callable.
_live: dict[str, Callable[[], None]] = {}


def register(sandbox_id: str, killer: Callable[[], None]) -> None:
    """Track a live sandbox; enforce the concurrency cap."""
    with _lock:
        limit = max_concurrent()
        if limit is not None and len(_live) >= limit:
            raise RuntimeError(
                f"Refusing to start sandbox: RAITBOX_MAX_SESSIONS={limit} reached "
                f"({len(_live)} live). Kill an existing sandbox first."
            )
        _live[sandbox_id] = killer


def unregister(sandbox_id: str) -> None:
    with _lock:
        _live.pop(sandbox_id, None)


def live_count() -> int:
    with _lock:
        return len(_live)


def _cleanup_at_exit() -> None:
    with _lock:
        leaked = dict(_live)
        _live.clear()
    if not leaked:
        return
    warnings.warn(
        f"raitbox: {len(leaked)} sandbox(es) were not killed explicitly; "
        f"stopping them now to avoid charges: {sorted(leaked)}",
        ResourceWarning,
        stacklevel=1,
    )
    for killer in leaked.values():
        with contextlib.suppress(Exception):  # best-effort: interpreter is exiting
            killer()


atexit.register(_cleanup_at_exit)
