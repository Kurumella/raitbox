"""Audit trail: CloudTrail lookup, source resolution, and simplification."""

from __future__ import annotations

import json
from typing import Any

from raitbox.core import audit


class FakeCloudTrail:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def lookup_events(self, **kwargs):
        self.calls.append(kwargs)
        idx = 1 if kwargs.get("NextToken") == "n1" else 0
        return self._pages[idx]


def _event(name: str, source: str, arn: str = "arn:aws:iam::1:user/dev") -> dict[str, Any]:
    return {
        "EventName": name,
        "EventSource": source,
        "EventTime": "2026-09-12T00:00:00Z",
        "Username": "dev",
        "EventId": f"id-{name}",
        "CloudTrailEvent": json.dumps(
            {"userIdentity": {"arn": arn}, "requestParameters": {"imageIdentifier": "arn:img"}}
        ),
    }


def test_source_resolution():
    assert audit._resolve_source("microvm") == "lambda-microvms.amazonaws.com"
    assert audit._resolve_source("agentcore") == "bedrock-agentcore.amazonaws.com"
    assert audit._resolve_source("s3") == "s3.amazonaws.com"
    assert audit._resolve_source("custom.amazonaws.com") == "custom.amazonaws.com"
    assert audit._resolve_source(None) is None


def test_lookup_filters_by_source_and_simplifies():
    ct = FakeCloudTrail(
        [
            {
                "Events": [
                    _event("RunMicrovm", "lambda-microvms.amazonaws.com"),
                    _event("GetObject", "s3.amazonaws.com"),  # filtered out
                ]
            }
        ]
    )
    events = audit.lookup_events(source="microvm", client=ct)
    assert [e["name"] for e in events] == ["RunMicrovm"]
    e = events[0]
    assert e["source"] == "lambda-microvms.amazonaws.com"
    assert e["username"] == "dev"
    assert e["request_parameters"] == {"imageIdentifier": "arn:img"}


def test_lookup_paginates_until_max_results():
    ct = FakeCloudTrail(
        [
            {"Events": [_event("RunMicrovm", "lambda-microvms.amazonaws.com")], "NextToken": "n1"},
            {"Events": [_event("TerminateMicrovm", "lambda-microvms.amazonaws.com")]},
        ]
    )
    events = audit.lookup_events(source="microvm", client=ct)
    assert [e["name"] for e in events] == ["RunMicrovm", "TerminateMicrovm"]


def test_event_name_attribute_passed_through():
    ct = FakeCloudTrail([{"Events": []}])
    audit.lookup_events(source=None, event_name="RunMicrovm", client=ct)
    assert ct.calls[0]["LookupAttributes"] == [
        {"AttributeKey": "EventName", "AttributeValue": "RunMicrovm"}
    ]
