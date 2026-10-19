"""What a member of a git project's room follows by itself: the room's work (C sources, headers, splits, configure.py's
Object entries). Anything else in the host's commits runs when the member builds the tree, so it waits until they
choose to follow it; a project that comes whole from the host needs the member's trust from the start."""

import os
import subprocess
from pathlib import Path

import pytest

from decompnet.app import forkroom
from decompnet.network import NetworkError

CONFIGURE = '''#!/usr/bin/env python3
from tools.project import Object, ProjectConfig, Matching, NonMatching, Equivalent

config = ProjectConfig()
config.libs = [
    {
        "lib": "game",
        "mw_version": "GC/1.3.2",
        "cflags": cflags_game,
        "objects": [
            Object(NonMatching, "game/main.c"),
            Object(NonMatching, "game/player.c", extra_cflags=["-O4,p"]),
        ],
    },
]
'''


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.org"}
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True, env=env).stdout.strip()


def _commit(root: Path, files: dict[str, str], message: str = "work") -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path):
    """A public decomp (its own repository) and the room host's clone of it, at the commit the room starts from."""
    public = tmp_path / "public"
    public.mkdir()
    _git(public, "init", "-q", "-b", "main")
    base = _commit(public, {"configure.py": CONFIGURE, "src/game/main.c": "int x;\n", "include/game.h": "",
                            "config/GALE01/splits.txt": "game/main.c:\n", "tools/project.py": "# the build\n"})
    host = tmp_path / "host"
    subprocess.run(["git", "clone", "-q", str(public), str(host)], check=True)
    return public, host, base


def _changes(host: Path, base: str, files: dict[str, str]) -> list[str]:
    return forkroom.build_changes(host, base, _commit(host, files))


def test_the_rooms_work_is_followed(project):
    public, host, base = project
    linked = CONFIGURE.replace('Object(NonMatching, "game/main.c")', 'Object(Matching, "game/main.c")')
    assert _changes(host, base, {"src/game/main.c": "int x = 1;\n", "include/game.h": "typedef int s32;\n",
                                 "config/GALE01/splits.txt": "game/main.c:\ngame/new.c:\n", "configure.py": linked}) == []


def test_a_new_file_listed_like_an_existing_one_is_work(project):
    public, host, base = project
    split = CONFIGURE.replace('Object(NonMatching, "game/player.c", extra_cflags=["-O4,p"]),',
                              'Object(NonMatching, "game/player.c", extra_cflags=["-O4,p"]),\n'
                              '            Object(NonMatching, "game/player_b.c", extra_cflags=["-O4,p"]),')
    linked = CONFIGURE.replace('Object(NonMatching, "game/main.c")', 'Object(MatchingFor("GALE01", "GALP01"), "game/main.c")')
    assert _changes(host, base, {"configure.py": split, "src/game/player_b.c": ""}) == []
    assert forkroom.build_changes(host, base, _commit(host, {"configure.py": linked})) == []


@pytest.mark.parametrize("edit", [
    # settings no entry had: the compiler runs them
    lambda c: c.replace('Object(NonMatching, "game/main.c")', 'Object(NonMatching, "game/main.c", extra_cflags=["-wrapper", "sh"])'),
    lambda c: c.replace('"game/main.c"),', '"game/main.c"),\n            Object(NonMatching, "x.c", mw_version=__import__("os").getcwd()),'),
    lambda c: c.replace("Object(NonMatching, \"game/main.c\")", "Object(MatchingFor(__import__(\"os\").getcwd()), \"game/main.c\")"),
    lambda c: c.replace('"cflags": cflags_game,', '"cflags": [*cflags_game, "-plugin"],'),  # outside the entries
    lambda c: c + "\nimport os\nos.system('echo hi')\n",
    lambda c: c.replace('"game/main.c"),', '"game/main.c"), __import__("os").getcwd(),'),  # between entries
])
def test_anything_else_in_configure_py_waits(project, edit):
    public, host, base = project
    assert _changes(host, base, {"configure.py": edit(CONFIGURE)}) == ["configure.py"]


@pytest.mark.parametrize("path", ["tools/project.py", "tools/helper.c", ".gitmodules", ".github/workflows/build.yml",
                                  "Makefile", "config/GALE01/config.yml", "config/GALE01/hack.h", "build.ninja",
                                  ".decomp.wtf.json", "orig/GALE01/.gitkeep"])
def test_the_build_setup_waits(project, path):
    public, host, base = project
    assert _changes(host, base, {path: "anything\n"}) == [path]


def test_a_source_file_that_is_a_link_waits(project):
    public, host, base = project
    os.symlink("/etc/hostname", host / "src" / "game" / "peek.c")
    assert _changes(host, base, {}) == ["src/game/peek.c"]


def _clone(tmp_path, public, host, base, **kw):
    dest = tmp_path / f"member{len(list(tmp_path.glob('member*')))}"
    info = {"repo_url": str(public), "base": base, "branch": _git(host, "rev-parse", "--abbrev-ref", "HEAD")}
    held = forkroom.clone(dest, info, lambda have: forkroom.bundle(host, have), log=lambda *_: None, **kw)
    return dest, held


def test_a_member_gets_the_rooms_work_and_waits_for_the_rest(project, tmp_path):
    public, host, base = project
    work = _commit(host, {"src/game/main.c": "int x = 2;\n"})
    dest, held = _clone(tmp_path, public, host, base)
    assert held is None and _git(dest, "rev-parse", "HEAD") == work
    tip = _commit(host, {"tools/project.py": "import os\n"})
    dest, held = _clone(tmp_path, public, host, base)
    assert held["head"] == tip and held["paths"] == ["tools/project.py"] and "import os" in held["diff"]
    assert _git(dest, "rev-parse", "HEAD") == base  # the public project as it is, not the room's commits
    dest, held = _clone(tmp_path, public, host, base, trust=True)
    assert held is None and _git(dest, "rev-parse", "HEAD") == tip


def test_a_project_that_comes_whole_from_the_host_needs_trust(project, tmp_path):
    public, host, base = project
    with pytest.raises(NetworkError, match="trust"):
        forkroom.clone(tmp_path / "m", {"repo_url": None, "base": None, "branch": "main"}, lambda have: b"",
                       log=lambda *_: None)
    assert not (tmp_path / "m").exists()  # nothing fetched


def test_a_member_proves_with_the_copy_the_project_keeps(tmp_path):
    """Where a project keeps its game is where a host (or a member joining again) finds its copy."""
    from decompnet.forks.project import ForkProject

    root = tmp_path / "p"
    (root / "extract").mkdir(parents=True)
    rom = os.urandom(70_000)
    (root / "extract" / "baserom.nds").write_bytes(rom)
    (root / ".decomp.wtf.json").write_text('{"game": {"path": "extract/baserom.nds"}}')
    fp = ForkProject(root)
    import hashlib
    cfg = {"program_sha1": hashlib.sha1(rom).hexdigest()}
    copy = forkroom.own_copy(fp, cfg)
    assert copy is not None and forkroom.proof("n", copy) == forkroom.proof("n", forkroom.in_memory(rom))
    assert forkroom.own_copy(fp, {"program_sha1": "0" * 40}) is None
