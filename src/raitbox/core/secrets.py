"""Resolve secret references into environment values at sandbox start.

A ``secrets`` mapping passed to ``Sandbox.create`` maps env-var names to
*references*, resolved just-in-time and merged into ``envs`` (so they land in the
sandbox environment without being hard-coded in your source):

  - ``secretsmanager:<id-or-arn>``            whole secret string
  - ``secretsmanager:<id-or-arn>::<json-key>``  one key of a JSON secret
  - ``ssm:<parameter-name>``                   SSM Parameter Store (SecureString ok)
  - anything else                              used as a literal value

Resolution uses the standard AWS credential chain; nothing is logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

_SM = "secretsmanager:"
_SSM = "ssm:"


def resolve_secrets(
    specs: dict[str, str],
    region_name: str | None = None,
    profile_name: str | None = None,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, str]:
    """Turn ``{ENV: reference}`` into ``{ENV: value}`` (see module docstring)."""
    if not specs:
        return {}
    factory: Callable[[str], Any]
    if client_factory is None:
        import boto3

        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        factory = cast("Callable[[str], Any]", session.client)
    else:
        factory = client_factory
    clients: dict[str, Any] = {}

    def client(svc: str) -> Any:
        if svc not in clients:
            clients[svc] = factory(svc)
        return clients[svc]

    out: dict[str, str] = {}
    for env_name, ref in specs.items():
        if ref.startswith(_SM):
            sid, sep, jkey = ref[len(_SM) :].partition("::")
            raw = client("secretsmanager").get_secret_value(SecretId=sid)["SecretString"]
            out[env_name] = json.loads(raw)[jkey] if sep else raw
        elif ref.startswith(_SSM):
            name = ref[len(_SSM) :]
            out[env_name] = client("ssm").get_parameter(Name=name, WithDecryption=True)[
                "Parameter"
            ]["Value"]
        else:
            out[env_name] = ref
    return out
