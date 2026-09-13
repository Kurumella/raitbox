"""Declarative image builder — define a MicroVM image's contents in Python.

Chain steps to describe what goes into your sandbox image, then hand the
:class:`Image` to :func:`raitbox.tooling.build_image.build_image` (``image=``).
The steps are appended to the RaitBox agent-server Dockerfile (after the base
setup, before the server entrypoint), so every built image still speaks the
agent-server contract.

A declarative, chainable image spec. Example::

    from raitbox.tooling.image import Image

    img = (
        Image.al2023()
        .dnf_install("git", "ripgrep")
        .pip_install("pandas", "numpy")
        .run_commands("useradd -m sandbox")
        .env("LANG", "C.UTF-8")
        .workdir("/workspace")
    )
    arn = build_image(name="data-sbx", code_bucket=..., build_role_arn=..., image=img)
"""

from __future__ import annotations

import shlex


class Image:
    """A declarative description of extra layers to bake into a sandbox image."""

    def __init__(self) -> None:
        self._steps: list[str] = []

    # -- named entry points (documentation sugar; the base is fixed today) --

    @classmethod
    def al2023(cls) -> Image:
        """Start from the RaitBox AL2023 agent-server base (the default)."""
        return cls()

    # Alias for people coming from other SDKs.
    debian_slim = al2023

    # -- fluent steps ------------------------------------------------------

    def pip_install(self, *packages: str) -> Image:
        if packages:
            joined = " ".join(shlex.quote(p) for p in packages)
            self._steps.append(f"RUN python3 -m pip install --no-cache-dir {joined}")
        return self

    def dnf_install(self, *packages: str) -> Image:
        if packages:
            joined = " ".join(shlex.quote(p) for p in packages)
            self._steps.append(f"RUN dnf install -y {joined} && dnf clean all")
        return self

    # Alias for Debian-family muscle memory (maps to the same layer intent).
    apt_install = dnf_install

    def run_commands(self, *commands: str) -> Image:
        self._steps.extend(f"RUN {c}" for c in commands)
        return self

    def env(self, key: str, value: str) -> Image:
        self._steps.append(f"ENV {key}={shlex.quote(value)}")
        return self

    def workdir(self, path: str) -> Image:
        self._steps.append(f"WORKDIR {path}")
        return self

    def copy(self, src: str, dest: str) -> Image:
        self._steps.append(f"COPY {src} {dest}")
        return self

    def dockerfile_lines(self) -> list[str]:
        """The extra Dockerfile lines this image contributes, in order."""
        return list(self._steps)
