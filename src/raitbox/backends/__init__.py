"""Backend registry.

Backends are imported lazily so that, for example, using the AgentCore Code
Interpreter backend does not require the optional ``bedrock-agentcore`` package
that the Runtime backend needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Backend

# Canonical name -> "module:ClassName".
_BACKENDS: dict[str, str] = {
    "agentcore-ci": "raitbox.backends.agentcore_ci:AgentCoreCodeInterpreterBackend",
    "agentcore-runtime": "raitbox.backends.agentcore_runtime:AgentCoreRuntimeBackend",
    "microvm": "raitbox.backends.microvm:MicroVMBackend",
}


def get_backend(name: str) -> type[Backend]:
    """Import and return the backend class for a canonical name."""
    import importlib

    spec = _BACKENDS.get(name)
    if spec is None:
        raise ValueError(f"Unknown backend {name!r}; valid: {sorted(_BACKENDS)}")
    module_path, class_name = spec.split(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


__all__ = ["get_backend"]
