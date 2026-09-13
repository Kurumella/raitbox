"""Sandbox lifecycle events → Amazon EventBridge (the RaitBox "webhooks").

Off by default. Call :func:`configure` (or set ``RAITBOX_EVENT_BUS`` to a bus
name) and RaitBox emits ``sandbox.created`` / ``sandbox.killed`` events to
EventBridge. From there, an EventBridge rule with an API-destination target
delivers them to any HTTPS webhook — an AWS-native webhook mechanism with no
polling in the SDK.

    from raitbox.core.events import configure
    configure(bus_name="default", source="my-agent")
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

_STATE: dict[str, Any] = {
    "enabled": False,
    "bus": "default",
    "source": "raitbox",
    "client": None,
    "region": None,
    "profile": None,
}


def configure(
    *,
    bus_name: str = "default",
    source: str = "raitbox",
    region_name: str | None = None,
    profile_name: str | None = None,
    client: Any = None,
    enabled: bool = True,
) -> None:
    """Enable event emission to an EventBridge bus."""
    _STATE.update(
        enabled=enabled,
        bus=bus_name,
        source=source,
        region=region_name,
        profile=profile_name,
    )
    if client is not None:
        _STATE["client"] = client


def _maybe_autoconfigure() -> None:
    if not _STATE["enabled"] and os.environ.get("RAITBOX_EVENT_BUS"):
        configure(bus_name=os.environ["RAITBOX_EVENT_BUS"])


def _client() -> Any:
    if _STATE["client"] is None:
        import boto3

        _STATE["client"] = boto3.Session(
            profile_name=_STATE["profile"], region_name=_STATE["region"]
        ).client("events")
    return _STATE["client"]


def emit(detail_type: str, detail: dict[str, Any]) -> None:
    """Emit one EventBridge event (best-effort; never raises to the caller)."""
    _maybe_autoconfigure()
    if not _STATE["enabled"]:
        return
    with contextlib.suppress(Exception):  # event delivery must not break sandbox ops
        _client().put_events(
            Entries=[
                {
                    "Source": _STATE["source"],
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                    "EventBusName": _STATE["bus"],
                }
            ]
        )


def reset() -> None:
    """Disable emission and drop the client (mainly for tests)."""
    _STATE.update(enabled=False, client=None, bus="default", source="raitbox")
