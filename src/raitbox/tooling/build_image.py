"""Build a Lambda MicroVM image containing the RaitBox agent server.

Zips the ``agent_server`` directory (Dockerfile + app), uploads it to S3, calls
``create_microvm_image``, and polls the image version build until it is
``SUCCESSFUL``. The resulting image ARN is what you pass as ``template=`` to
``Sandbox.create(backend="microvm", template=...)``.

Usage::

    python -m raitbox.tooling.build_image \\
        --name raitbox-sandbox \\
        --code-bucket my-build-bucket \\
        --build-role-arn arn:aws:iam::<acct>:role/raitbox-build-role \\
        --region us-east-1

Add ``--egress-connector <ARN>`` (repeatable) when the image's build steps fetch
from the internet (dnf/pip/EPEL).
"""

from __future__ import annotations

import argparse
import io
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:
    from .image import Image

_AGENT_SERVER_DIR = Path(__file__).resolve().parent.parent / "agent_server"
# Image `state` values (from the lambda-microvms service model).
_SUCCESS_STATES = {"CREATED", "UPDATED"}
_FAILED_STATES = {"CREATE_FAILED", "UPDATE_FAILED"}
_APP_PORT = 8080
_CUSTOM_LAYER_MARKER = "# >>> raitbox custom layers <<<"


def _render_dockerfile(image: Image | None) -> str:
    """The base agent-server Dockerfile with any declarative steps injected."""
    text = (_AGENT_SERVER_DIR / "Dockerfile").read_text()
    if image is None:
        return text
    steps = image.dockerfile_lines()
    if not steps:
        return text
    injected = _CUSTOM_LAYER_MARKER + "\n" + "\n".join(steps)
    return text.replace(_CUSTOM_LAYER_MARKER, injected, 1)


def _zip_agent_server(image: Image | None = None) -> bytes:
    """Zip the agent-server build context (Dockerfile at the archive root)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Dockerfile", _render_dockerfile(image))
        for name in ("requirements.txt", "server.py"):
            zf.write(_AGENT_SERVER_DIR / name, arcname=name)
    return buf.getvalue()


def build_image(
    *,
    name: str,
    code_bucket: str,
    build_role_arn: str,
    region: str | None = None,
    profile: str | None = None,
    base_image_arn: str | None = None,
    code_key: str | None = None,
    ready_timeout_seconds: int = 300,
    egress_connectors: list[str] | None = None,
    image: Image | None = None,
    poll_interval: float = 10,
    poll_deadline: float = 1800,
) -> str:
    """Build the image and return its ARN once the version is SUCCESSFUL.

    Pass a declarative :class:`~raitbox.tooling.image.Image` as ``image=`` to
    bake extra packages/commands into the sandbox on top of the agent server.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    resolved_region = session.region_name or region
    s3 = session.client("s3")
    mv = session.client("lambda-microvms")

    key = code_key or f"raitbox/{name}.zip"
    s3.put_object(Bucket=code_bucket, Key=key, Body=_zip_agent_server(image))
    code_uri = f"s3://{code_bucket}/{key}"

    base = base_image_arn or f"arn:aws:lambda:{resolved_region}:aws:microvm-image:al2023-1"

    params: dict[str, Any] = {
        "name": name,
        "codeArtifact": {"uri": code_uri},
        "baseImageArn": base,
        "buildRoleArn": build_role_arn,
        # Hooks the agent server implements; ports/timeouts per the service model.
        "hooks": {
            "port": _APP_PORT,
            "microvmImageHooks": {
                "ready": "ENABLED",
                "readyTimeoutInSeconds": ready_timeout_seconds,
            },
            "microvmHooks": {
                "run": "ENABLED",
                "resume": "ENABLED",
                "suspend": "ENABLED",
                "terminate": "ENABLED",
            },
        },
    }
    # The build runs the Dockerfile (dnf/pip), which needs egress to the internet.
    if egress_connectors:
        params["egressNetworkConnectors"] = egress_connectors
    resp = mv.create_microvm_image(**params)
    image_arn = resp["imageArn"]
    _wait_build(mv, image_arn, poll_interval, poll_deadline)
    return image_arn


def _wait_build(mv: Any, image_arn: str, interval: float, deadline: float) -> None:
    end = time.time() + deadline
    while time.time() < end:
        info = mv.get_microvm_image(imageIdentifier=image_arn)
        state = info.get("state")
        if state in _SUCCESS_STATES:
            return
        if state in _FAILED_STATES:
            reason = info.get("latestFailedImageVersion")
            raise RuntimeError(f"MicroVM image build FAILED for {image_arn} ({state}): {reason}")
        time.sleep(interval)
    raise TimeoutError(f"MicroVM image {image_arn} did not finish building in time")


def list_images(
    *,
    region: str | None = None,
    profile: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """List built MicroVM images (a snapshot registry) as ``{name, arn, state}``."""
    if client is None:
        client = boto3.Session(profile_name=profile, region_name=region).client("lambda-microvms")
    out: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        params = {"nextToken": next_token} if next_token else {}
        resp = client.list_microvm_images(**params)
        for img in resp.get("items", []):
            out.append(
                {
                    "name": img.get("name"),
                    "arn": img.get("imageArn"),
                    "state": img.get("state"),
                }
            )
        next_token = resp.get("nextToken")
        if not next_token:
            return out


def delete_image(
    image_arn: str,
    *,
    region: str | None = None,
    profile: str | None = None,
    client: Any = None,
) -> None:
    """Delete a built MicroVM image by ARN (registry cleanup)."""
    if client is None:
        client = boto3.Session(profile_name=profile, region_name=region).client("lambda-microvms")
    client.delete_microvm_image(imageIdentifier=image_arn)


def main() -> None:
    p = argparse.ArgumentParser(description="Build an RaitBox MicroVM image")
    p.add_argument("--name", required=True)
    p.add_argument("--code-bucket", required=True)
    p.add_argument("--build-role-arn", required=True)
    p.add_argument("--region")
    p.add_argument("--profile")
    p.add_argument("--base-image-arn")
    p.add_argument(
        "--egress-connector",
        action="append",
        dest="egress_connectors",
        metavar="ARN",
        help="Network connector ARN giving the build internet access (repeatable). "
        "Needed when the Dockerfile fetches from the internet (dnf/pip/EPEL); e.g. "
        "arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
    )
    args = p.parse_args()
    arn = build_image(
        name=args.name,
        code_bucket=args.code_bucket,
        build_role_arn=args.build_role_arn,
        region=args.region,
        profile=args.profile,
        base_image_arn=args.base_image_arn,
        egress_connectors=args.egress_connectors,
    )
    print(arn)


if __name__ == "__main__":
    main()
