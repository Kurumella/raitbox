"""Declarative Image builder + snapshot registry."""

from __future__ import annotations

import sys
from typing import Any

from raitbox.tooling import build_image as bi
from raitbox.tooling.image import Image


def test_image_fluent_steps_order_and_quoting():
    img = (
        Image.al2023()
        .dnf_install("git", "ripgrep")
        .pip_install("pandas", "numpy==2.0")
        .run_commands("useradd -m sandbox")
        .env("LANG", "C.UTF-8")
        .workdir("/workspace")
        .copy("./local", "/opt/app")
    )
    lines = img.dockerfile_lines()
    assert lines == [
        "RUN dnf install -y git ripgrep && dnf clean all",
        "RUN python3 -m pip install --no-cache-dir pandas numpy==2.0",
        "RUN useradd -m sandbox",
        "ENV LANG=C.UTF-8",
        "WORKDIR /workspace",
        "COPY ./local /opt/app",
    ]


def test_render_dockerfile_injects_at_marker():
    img = Image().pip_install("requests")
    rendered = bi._render_dockerfile(img)
    assert "RUN python3 -m pip install --no-cache-dir requests" in rendered
    # Injected before the server entrypoint, after the base setup.
    assert rendered.index("pip install --no-cache-dir requests") < rendered.index("CMD [")
    # Base contract is preserved.
    assert "uvicorn" in rendered and "EXPOSE 8080" in rendered


def test_render_dockerfile_no_image_is_untouched():
    base = bi._render_dockerfile(None)
    assert bi._CUSTOM_LAYER_MARKER in base  # marker stays; nothing injected


def test_zip_includes_rendered_dockerfile():
    import io
    import zipfile

    data = bi._zip_agent_server(Image().dnf_install("htop"))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert {"Dockerfile", "requirements.txt", "server.py"} <= names
        assert "htop" in zf.read("Dockerfile").decode()


class FakeMV:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def list_microvm_images(self, **kw: Any):
        if kw.get("nextToken") == "p2":
            return {"items": [{"imageArn": "arn:b", "name": "b", "state": "CREATED"}]}
        return {
            "items": [{"imageArn": "arn:a", "name": "a", "state": "CREATED"}],
            "nextToken": "p2",
        }

    def delete_microvm_image(self, imageIdentifier: str):
        self.deleted.append(imageIdentifier)


def test_list_images_paginates():
    imgs = bi.list_images(client=FakeMV())
    assert [i["arn"] for i in imgs] == ["arn:a", "arn:b"]
    assert imgs[0] == {"name": "a", "arn": "arn:a", "state": "CREATED"}


def test_delete_image():
    mv = FakeMV()
    bi.delete_image("arn:a", client=mv)
    assert mv.deleted == ["arn:a"]


def test_cli_passes_egress_connectors(monkeypatch):
    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return "arn:built"

    monkeypatch.setattr(bi, "build_image", fake_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_image",
            "--name",
            "img",
            "--code-bucket",
            "b",
            "--build-role-arn",
            "arn:role",
            "--egress-connector",
            "arn:egr:1",
            "--egress-connector",
            "arn:egr:2",
        ],
    )
    bi.main()
    assert captured["egress_connectors"] == ["arn:egr:1", "arn:egr:2"]
    assert captured["name"] == "img"


def test_cli_egress_defaults_to_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(bi, "build_image", lambda **k: captured.update(k) or "arn")
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_image", "--name", "i", "--code-bucket", "b", "--build-role-arn", "r"],
    )
    bi.main()
    assert captured["egress_connectors"] is None
