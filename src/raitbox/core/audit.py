"""Audit trail for sandbox activity, read from AWS CloudTrail.

Every sandbox that RaitBox provisions is created through AWS control-plane APIs
(``lambda-microvms``, ``bedrock-agentcore``), so AWS records them in CloudTrail
automatically — no extra logging in the SDK. :func:`lookup_events` queries that
trail and returns simplified records (who did what, when), giving you a tamper-
evident audit log for sandbox lifecycle operations.

    from raitbox.core.audit import lookup_events
    for e in lookup_events(source="lambda-microvms", hours=24):
        print(e["time"], e["name"], e["username"])
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

# Control-plane API sources RaitBox drives, per backend.
KNOWN_SOURCES = {
    "microvm": "lambda-microvms.amazonaws.com",
    "agentcore": "bedrock-agentcore.amazonaws.com",
}


def _resolve_source(source: str | None) -> str | None:
    if source is None:
        return None
    if source in KNOWN_SOURCES:
        return KNOWN_SOURCES[source]
    return source if source.endswith(".amazonaws.com") else f"{source}.amazonaws.com"


def lookup_events(
    *,
    source: str | None = "microvm",
    event_name: str | None = None,
    hours: float = 24,
    start_time: _dt.datetime | None = None,
    end_time: _dt.datetime | None = None,
    max_results: int = 50,
    region_name: str | None = None,
    profile_name: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Return simplified CloudTrail events for RaitBox sandbox operations.

    ``source`` accepts a backend alias (``"microvm"``/``"agentcore"``), a bare
    service name, or a full event source; ``event_name`` filters to one API call
    (e.g. ``"RunMicrovm"``). Each result is ``{name, source, time, username,
    event_id, request_parameters}``.
    """
    if client is None:
        import boto3

        client = boto3.Session(profile_name=profile_name, region_name=region_name).client(
            "cloudtrail"
        )

    now = _dt.datetime.now(_dt.timezone.utc)
    kwargs: dict[str, Any] = {
        "StartTime": start_time or (now - _dt.timedelta(hours=hours)),
        "EndTime": end_time or now,
    }
    attrs = []
    resolved = _resolve_source(source)
    if event_name:
        attrs.append({"AttributeKey": "EventName", "AttributeValue": event_name})
    if attrs:
        kwargs["LookupAttributes"] = attrs

    out: list[dict[str, Any]] = []
    next_token: str | None = None
    while len(out) < max_results:
        if next_token:
            kwargs["NextToken"] = next_token
        resp = client.lookup_events(**kwargs)
        for ev in resp.get("Events", []):
            record = _simplify(ev)
            # LookupEvents can't filter by source server-side, so filter here.
            if resolved and record["source"] != resolved:
                continue
            out.append(record)
            if len(out) >= max_results:
                break
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return out


def _simplify(ev: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    raw = ev.get("CloudTrailEvent")
    if raw:
        try:
            detail = json.loads(raw)
        except (ValueError, TypeError):
            detail = {}
    return {
        "name": ev.get("EventName"),
        "source": ev.get("EventSource") or detail.get("eventSource"),
        "time": ev.get("EventTime"),
        "username": ev.get("Username") or detail.get("userIdentity", {}).get("arn"),
        "event_id": ev.get("EventId"),
        "request_parameters": detail.get("requestParameters"),
    }
