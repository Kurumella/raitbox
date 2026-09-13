"""Pause/resume via S3 file snapshots.

The AWS backends do not expose a native "snapshot this live session" API to the
client, so RaitBox implements a pseudo-pause: it copies the sandbox *files* to
S3, then (on resume) restores them into a fresh sandbox. This mirrors the E2B
``pause()``/``resume()`` surface with an important caveat — **files survive,
in-memory/kernel state does not**.

Requires an S3 bucket via ``bucket=`` or ``RAITBOX_SNAPSHOT_BUCKET`` and
``s3:GetObject``/``s3:PutObject``/``s3:ListBucket`` on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from .errors import InvalidArgumentException
from .settings import snapshot_bucket

if TYPE_CHECKING:
    from ..backends.base import Backend

# Directories that are never worth snapshotting.
_SKIP_DIRS = {"proc", "sys", "dev", ".git"}
_MAX_DEPTH = 32


def _walk(backend: Backend, path: str, depth: int) -> list[str]:
    """Return every file path under ``path`` (best-effort recursive listing)."""
    if depth > _MAX_DEPTH:
        return []
    files: list[str] = []
    for entry in backend.file_list(path):
        if entry.name in _SKIP_DIRS:
            continue
        child = entry.path or (f"{path.rstrip('/')}/{entry.name}" if path else entry.name)
        if entry.type == "dir":
            files.extend(_walk(backend, child, depth + 1))
        else:
            files.append(child)
    return files


def save_snapshot(backend: Backend, *, bucket: str | None = None, path: str = "") -> str:
    """Copy the sandbox's files to S3 under the sandbox id; return the id."""
    resolved = snapshot_bucket(bucket)
    if not resolved:
        raise InvalidArgumentException(
            "pause() needs an S3 bucket: pass bucket= or set RAITBOX_SNAPSHOT_BUCKET"
        )
    s3 = boto3.client("s3")
    sandbox_id = backend.sandbox_id
    for file_path in _walk(backend, path, 0):
        data = backend.file_read(file_path, format="bytes")
        body = bytes(data) if isinstance(data, (bytes, bytearray)) else data.encode()
        s3.put_object(
            Bucket=resolved,
            Key=f"raitbox-snapshots/{sandbox_id}/{file_path.lstrip('/')}",
            Body=body,
            Metadata={"origpath": file_path},
        )
    return sandbox_id


def load_snapshot(backend: Backend, sandbox_id: str, *, bucket: str | None = None) -> None:
    """Restore files from an S3 snapshot into a (fresh) sandbox."""
    resolved = snapshot_bucket(bucket)
    if not resolved:
        raise InvalidArgumentException(
            "resume() needs an S3 bucket: pass bucket= or set RAITBOX_SNAPSHOT_BUCKET"
        )
    s3 = boto3.client("s3")
    prefix = f"raitbox-snapshots/{sandbox_id}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=resolved, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            head = s3.get_object(Bucket=resolved, Key=key)
            orig = head.get("Metadata", {}).get("origpath") or key[len(prefix) :]
            backend.file_write(orig, head["Body"].read())
