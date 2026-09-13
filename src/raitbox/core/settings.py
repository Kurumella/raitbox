"""Backend selection and shared AWS configuration.

Backend is chosen (in priority order) by the ``backend=`` kwarg to
``Sandbox.create()``, else the ``RAITBOX_BACKEND`` env var, else the default
``agentcore-ci``. Credentials use the standard AWS chain; ``region_name`` and
``profile_name`` may be passed through for a boto3 ``Session``.
"""

from __future__ import annotations

import os

DEFAULT_BACKEND = "agentcore-ci"

# Accepted aliases -> canonical backend name (see backends/__init__.py).
_ALIASES = {
    "agentcore": "agentcore-ci",
    "agentcore-ci": "agentcore-ci",
    "agentcore-code-interpreter": "agentcore-ci",
    "code-interpreter": "agentcore-ci",
    "agentcore-runtime": "agentcore-runtime",
    "runtime": "agentcore-runtime",
    "microvm": "microvm",
    "lambda-microvm": "microvm",
    "lambda": "microvm",
}


def resolve_backend(backend: str | None) -> str:
    """Return the canonical backend name from a kwarg or env override."""
    name = backend or os.environ.get("RAITBOX_BACKEND") or DEFAULT_BACKEND
    key = name.strip().lower()
    if key not in _ALIASES:
        valid = sorted(set(_ALIASES.values()))
        raise ValueError(f"Unknown backend {name!r}; choose one of {valid}")
    return _ALIASES[key]


def max_concurrent() -> int | None:
    """Concurrent-session cap from ``RAITBOX_MAX_SESSIONS`` (None = unlimited)."""
    raw = os.environ.get("RAITBOX_MAX_SESSIONS")
    return int(raw) if raw else None


def snapshot_bucket(explicit: str | None = None) -> str | None:
    """S3 bucket for pause/resume snapshots (kwarg overrides env)."""
    return explicit or os.environ.get("RAITBOX_SNAPSHOT_BUCKET")
