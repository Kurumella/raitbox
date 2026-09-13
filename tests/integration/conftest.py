"""Fixtures and gating for LIVE integration tests.

These tests hit real AWS and bill short sessions. They only run when:
  1. invoked explicitly with ``-m integration`` (see pyproject ``addopts``), AND
  2. AWS credentials + a region are available, AND
  3. the per-backend env vars they need are set (else that module skips).

Every test is responsible for killing what it creates; the shared fixtures use
``try/finally`` so a failing assertion still tears the sandbox down.

Env vars (see docs/INTEGRATION.md):
  AWS creds via the normal chain; region via AWS_DEFAULT_REGION / AWS_REGION.
  RAITBOX_IT_MICROVM_IMAGE   image ARN for the microvm backend
  RAITBOX_IT_EXECUTION_ROLE  (optional) MicroVM execution role ARN
  RAITBOX_IT_EGRESS          (optional) egress network-connector ARN
  RAITBOX_IT_RUNTIME_ARN     agentRuntimeArn for the agentcore-runtime backend
"""

from __future__ import annotations

import os

import pytest


def _region() -> str | None:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")


def _aws_usable() -> bool:
    """True only if credentials actually work (verified via STS), so an
    unconfigured or stale-credential environment SKIPS rather than errors."""
    try:
        import boto3

        session = boto3.Session()
        if session.get_credentials() is None:
            return False
        session.client("sts", region_name=_region() or "us-east-1").get_caller_identity()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def region() -> str:
    r = _region()
    if not r:
        pytest.skip("no AWS region (set AWS_DEFAULT_REGION)")
    return r


@pytest.fixture(scope="session", autouse=True)
def _require_aws() -> None:
    """Skip the whole integration suite unless AWS creds actually work."""
    if not _aws_usable():
        pytest.skip("no usable AWS credentials (STS get-caller-identity failed)")


def env_or_skip(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value
