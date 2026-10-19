"""A toolchain whose project publishes no build for this computer is built from its pinned source when installed:
its build commands run in the source folder, then the binaries they made are kept and the source goes. WLA-DX is
pinned that way (its project publishes Windows builds only)."""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tarfile

import pytest

from decompnet import toolchains

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake build runs sh")


def _source(path) -> str:
    """A source tarball (under a top folder, as GitHub's are) whose build makes out/tool."""
    script = b"mkdir -p out && printf '#!/bin/sh\\necho built\\n' > out/tool\n"
    with tarfile.open(path, "w:gz") as t:
        info = tarfile.TarInfo("tool-1.0/build.sh")
        info.size = len(script)
        t.addfile(info, io.BytesIO(script))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_toolchain_is_built_from_its_source(tmp_path, monkeypatch):
    archive = tmp_path / "v1.0.tar.gz"
    digest = _source(archive)
    tc = toolchains.Toolchain("faketool", "Fake tool", "MIT", "fake/tool v1.0", kind="tool", parts=[{
        "url": "https://example.invalid/v1.0.tar.gz", "sha256": digest, "subdir": "tool-1.0",
        "build": [["sh", "build.sh"]], "binaries": ["out/tool"]}])
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    monkeypatch.setattr(toolchains, "get", lambda tid: tc)
    monkeypatch.setattr(toolchains, "_download", lambda url, to, log: shutil.copyfile(archive, to))
    dest = toolchains.install("faketool", log=lambda *_: None)
    assert sorted(p.name for p in dest.iterdir()) == [toolchains.MANIFEST, "tool"]  # (the source is gone)
    assert (dest / "tool").read_text().strip().endswith("echo built")
    assert toolchains.installed("faketool")["parts"][0]["sha256"] == digest


def test_a_build_that_fails_says_why_and_leaves_nothing(tmp_path, monkeypatch):
    archive = tmp_path / "v1.0.tar.gz"
    digest = _source(archive)
    tc = toolchains.Toolchain("faketool", "Fake tool", "MIT", "fake/tool v1.0", kind="tool", parts=[{
        "url": "https://example.invalid/v1.0.tar.gz", "sha256": digest, "subdir": "tool-1.0",
        "build": [["sh", "-c", "echo no compiler here; exit 3"]], "binaries": ["out/tool"]}])
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    monkeypatch.setattr(toolchains, "get", lambda tid: tc)
    monkeypatch.setattr(toolchains, "_download", lambda url, to, log: shutil.copyfile(archive, to))
    with pytest.raises(toolchains.ToolchainError, match="no compiler here"):
        toolchains.install("faketool", log=lambda *_: None)
    assert not toolchains.path("faketool").exists()


def test_wla_dx_is_pinned_from_source_and_for_windows():
    tc = toolchains.get("wla-dx")
    assert tc.license == "GPL-2.0-or-later"
    unix = tc.parts_for("linux-x86_64")[0]
    assert unix["build"] and unix["binaries"] == ["build/binaries/wla-z80", "build/binaries/wlalink"]
    assert "build" not in tc.parts_for("windows-x86_64")[0]


def test_cc65_is_pinned_from_source():
    tc = toolchains.get("cc65")
    assert tc.license == "Zlib"
    part = tc.parts_for("linux-x86_64")[0]
    assert part["build"] == [["make", "-C", "src", "ca65", "ld65"]] and part["binaries"] == ["bin/ca65", "bin/ld65"]
