"""Exercise the real shell installer with stand-in downloads/tools and no actual installation."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from decompnet import __version__

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def installer(tmp_path):
    script = tmp_path / "install.sh"
    shutil.copyfile(ROOT / "site/public/install.sh", script)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    curl_calls = tmp_path / "downloads"
    (bindir / "uv").write_text(
        '#!/bin/sh\n'
        '[ "$1" = --version ] && { echo "uv test"; exit 0; }\n'
        '[ "$1 $2" = "tool dir" ] && { echo "$TEST_BIN"; exit 0; }\n'
        'printf "uv %s\\n" "$*" >> "$TEST_CALLS"\n')
    (bindir / "decomp").write_text('#!/bin/sh\nprintf "decomp %s\\n" "$*" >> "$TEST_CALLS"\n')
    (bindir / "curl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_DOWNLOADS"\n'
        'case "$*" in *commits/*) echo \'{"sha":"1111111111111111111111111111111111111111"}\'; exit 0;; esac\n'
        'exit "${TEST_WHEEL_MISSING:-0}"\n')
    for path in bindir.iterdir():
        path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               TEST_BIN=str(bindir), TEST_CALLS=str(calls), TEST_DOWNLOADS=str(curl_calls))
    for name in ("DECOMP_SOURCE", "DECOMP_BRANCH", "DECOMP_WHEEL"):
        env.pop(name, None)
    return script, env, calls, curl_calls


def run_install(installer, source):
    script, env, calls, downloads = installer
    result = subprocess.run(["bash", str(script), "--no-start", "--source", source],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return calls.read_text(), downloads.read_text() if downloads.exists() else ""


def test_stable_installer_uses_the_fork_release(installer):
    calls, downloads = run_install(installer, "release")
    assert f"https://github.com/SynapticDreams/decomp.wtf/releases/download/v{__version__}/decompnet-{__version__}-py3-none-any.whl" in calls
    assert "install-info --channel release --branch  --commit  --repo SynapticDreams/decomp.wtf" in calls
    assert "decomp.wtf/dl/" not in downloads


def test_source_only_release_falls_back_to_its_tag(installer):
    installer[1]["TEST_WHEEL_MISSING"] = "1"
    calls, _ = run_install(installer, "release")
    assert f"https://github.com/SynapticDreams/decomp.wtf/archive/refs/tags/v{__version__}.tar.gz" in calls


def test_branch_installer_and_metadata_follow_the_fork(installer):
    calls, downloads = run_install(installer, "branch:main")
    assert "https://api.github.com/repos/SynapticDreams/decomp.wtf/commits/main" in downloads
    assert "https://github.com/SynapticDreams/decomp.wtf/archive/" + "1" * 40 + ".tar.gz" in calls
    assert "--channel branch --branch main --commit " + "1" * 40 + " --repo SynapticDreams/decomp.wtf" in calls


def test_container_marker_uses_bundled_version_or_explicit_override(tmp_path):
    # Exercise the generated entrypoint's version decision without starting an app.
    source = (ROOT / "site/public/linux-base.sh").read_text()
    entrypoint = source.split("cat > /usr/local/bin/decomp-entrypoint <<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    start = entrypoint.index('bundled_version=')
    end = entrypoint.index('\nif [ ! -x', start)
    installer = tmp_path / "install.sh"
    shutil.copyfile(ROOT / "site/public/install.sh", installer)
    decision = entrypoint[start:end].replace('/usr/local/share/decomp-wtf/install.sh', '"$TEST_INSTALLER"')
    script = decision + '\nprintf "%s" "$install_id"\n'
    env = dict(os.environ, TEST_INSTALLER=str(installer))
    env.pop("DECOMP_INSTALL_ID", None)
    assert subprocess.check_output(["sh", "-c", script], env=env, text=True) == __version__
    env["DECOMP_INSTALL_ID"] = "explicit-marker"
    assert subprocess.check_output(["sh", "-c", script], env=env, text=True) == "explicit-marker"
