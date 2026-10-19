"""Rooms for projects that are git source trees: GameCube, Wii and DS games started from scratch, and forks of public
decomps. The host's tree is the room's truth.

Every member works in their own copy of the tree, with their own copy of the game and their own compilers: an agent
compiles and diffs on its own computer (the game's code never travels). What travels is the work, as in rooms for
ROM projects (collab.py):
- claims go through the host, so nobody does the same function twice;
- a member's match is sent to the host, which builds it in its own tree against its own copy and commits it;
- edits of other files (headers, new source files, split files) are made by the host, which checks them the same way;
- members follow the host's commits as git bundles: only what the repository tracks, never the game, its extracted
  files or the build (those are gitignored in every project decomp.wtf makes or forks).
A public decomp's members get the project from its own host (GitHub, ...) at the commit the room's host started from;
the bundle then only holds the room's own commits.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..forks import policy as contribution_policy
from ..forks import dtksplits, scratch_dsd, scratch_dtk
from ..forks.project import EDITABLE, FORBIDDEN_PARTS, ForkError, ForkProject
from ..network import NetworkError

MAX_BUNDLE = 512 * 1024 * 1024


def _git(root: Path, *args: str, check: bool = True, timeout: float = 600) -> str:
    res = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=root, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    if check and res.returncode != 0:
        raise NetworkError(f"git {args[0]} failed: {(res.stderr or res.stdout).strip()[-400:]}")
    return res.stdout.strip()


Game = tuple[int, Callable[[int, int], bytes]]  # a game's bytes: (size, read(offset, n))


def in_memory(data: bytes) -> Game:
    return len(data), lambda at, n: bytes(data[at:at + n])


def in_file(path: Path) -> Game:
    """A file's bytes, read in place a stretch at a time (a disc is never read whole)."""
    def read(at: int, n: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(at)
            return f.read(n)
    return Path(path).stat().st_size, read


def game_bytes(path: Path) -> Game:
    """The bytes that say which game a file is, whatever form it comes in: a DS ROM's own bytes; a GameCube/Wii
    disc's main program (any disc format; a DOL on its own too); any other game file's bytes."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(0x440)
    if not scratch_dsd.is_nds(head):
        try:
            if scratch_dtk.is_dol(head if path.stat().st_size >= 64 << 20 else path.read_bytes()) or \
                    scratch_dtk.disc_kind(head) is not None:
                return in_memory(scratch_dtk.game_info(path)["dol"])
        except ForkError:
            pass
    return in_file(path)


def game_identity(path: Path) -> str:
    """Which game a file is (game_bytes), as a sha1."""
    return _sha1(game_bytes(path))


def _sha1(game: Game) -> str:
    size, read = game
    h = hashlib.sha1()
    for at in range(0, size, 1 << 20):
        h.update(read(at, 1 << 20))
    return h.hexdigest()


PROOF_WHOLE, PROOF_STRETCHES, PROOF_STRETCH = 64 << 20, 256, 4096


def proof(nonce: str, game: Game) -> str:
    """That this computer has the game, without sending any of it: an HMAC, keyed by the host's one-time nonce, over
    the game's size and bytes: all of them, or for a bigger game (a DS ROM) 256 stretches of 4 KiB at places the nonce
    picks. A game's sha1 is no proof (catalogues of dumps publish them); this takes the bytes. The host answers the
    same with its own copy and compares."""
    size, read = game
    key = nonce.encode()
    mac = hmac.new(key, size.to_bytes(8, "big"), hashlib.sha256)
    if size <= PROOF_WHOLE:
        for at in range(0, size, 1 << 20):
            mac.update(read(at, 1 << 20))
        return mac.hexdigest()
    for i in range(PROOF_STRETCHES):
        at = int.from_bytes(hmac.digest(key, i.to_bytes(4, "big"), "sha256")[:8], "big") % (size - PROOF_STRETCH + 1)
        mac.update(read(at, PROOF_STRETCH))
    return mac.hexdigest()


_copies: dict[tuple, tuple[str, Game]] = {}  # (file, size, mtime) -> (identity, its bytes): a disc's DOL is read once


def own_copy(fp: ForkProject, cfg: dict) -> Game | None:
    """This computer's copy of a git project's game, where the project keeps it (None: it isn't there, e.g. a file
    the setup took from a disc image, which is then the identity's source no more)."""
    want = project_identity(cfg)
    try:
        rel = fp.needs(cfg.get("version")).get("path") if want else None
    except (OSError, ForkError):
        rel = None
    if not rel:
        return None
    where = fp.root / rel
    files = sorted(p for p in where.iterdir() if p.is_file()) if rel.endswith("/") and where.is_dir() else [where]
    for p in files:
        try:
            st = p.stat()
            key = (str(p), st.st_size, st.st_mtime_ns)
            if key not in _copies:
                game = game_bytes(p)
                _copies[key] = (_sha1(game), game)
        except (OSError, ForkError):
            continue
        if _copies[key][0] == want:
            return _copies[key][1]
    return None


def project_identity(cfg: dict) -> str | None:
    """The game a project is for, as game_identity says it (None: not known, the build checks it)."""
    return (cfg.get("rom") or {}).get("sha1") or cfg.get("program_sha1") or cfg.get("game_sha1")


# ---------------------------------------------------------------------------------------------- host

def head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


def branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")


def base(root: Path) -> str | None:
    """The public decomp's commit the room's work starts from: a shallow clone's oldest commit, or in a full clone
    where the fork left the project's own branch (members check the room's commits against it: build_changes)."""
    shallow = Path(root) / ".git" / "shallow"
    if not shallow.exists():
        upstream = _git(root, "rev-parse", "-q", "--verify", "refs/remotes/origin/HEAD", check=False)
        return _git(root, "merge-base", "HEAD", upstream, check=False) or None if upstream else None
    roots = _git(root, "rev-list", "--max-parents=0", "HEAD").split()
    return roots[0] if roots else None


def bundle(root: Path, have: str = "") -> bytes:
    """The host's commits a member doesn't have yet (all of them for a new member), as a git bundle. Empty when the
    member is up to date."""
    root = Path(root)
    tip, ref = head(root), branch(root)
    if have == tip:
        return b""
    start, first = "", base(root)  # a public decomp's members have its commits from its own host
    if have and is_ancestor(root, have, tip):
        start = have
    elif first:
        if first == tip:  # nothing on top of the public decomp yet
            return b""
        start = first
    with tempfile.TemporaryDirectory(prefix="decomp-room-") as tmp:
        out = Path(tmp) / "room.bundle"
        _git(root, "bundle", "create", str(out), f"{start}..{ref}" if start else ref)
        data = out.read_bytes()
    if len(data) > MAX_BUNDLE:
        raise NetworkError("the project's history is too large to send")
    return data


def hello(net, slug: str) -> dict:
    """What a member needs to know about a git project's room before joining (no game data)."""
    proj, fp, cfg = net.forks.get(slug)
    return {"kind": "fork", "title": proj["name"], "game_code": cfg.get("version") or "", "region": "",
            "platform": cfg.get("platform"), "origin": cfg.get("origin") or "public", "repo_url": cfg.get("repo_url"),
            "version": cfg.get("version"), "fork_kind": cfg.get("fork_kind"), "sha1": project_identity(cfg),
            "compiler": cfg.get("compiler"), "base": base(fp.root) if cfg.get("repo_url") else None,
            "head": head(fp.root), "branch": branch(fp.root)}


# ---------------------------------------------------------------------------------------------- member

def fetch_bundle(root: Path, data: bytes, ref: str) -> str:
    """Take the host's commits from a bundle; returns the host's head."""
    with tempfile.TemporaryDirectory(prefix="decomp-room-") as tmp:
        f = Path(tmp) / "room.bundle"
        f.write_bytes(data)
        _git(root, "fetch", "-q", str(f), "+refs/heads/*:refs/remotes/room/*")
    return _git(root, "rev-parse", f"refs/remotes/room/{ref}")


def is_ancestor(root: Path, old: str, new: str) -> bool:
    return subprocess.run(["git", "-c", "safe.directory=*", "merge-base", "--is-ancestor", old, new], cwd=root,
                          capture_output=True).returncode == 0


def verifiable(info: dict) -> bool:
    """Whether a member can tell the room's work from the rest of its tree: a public decomp's room starts from a
    commit of the project's own repository, which the member gets from there; a project started from scratch (or a
    room without that commit) comes whole from the host, its build setup too."""
    return bool(info.get("repo_url") and info.get("base"))


def clone(dest: Path, info: dict, get_bundle, log=print, trust: bool = False) -> dict | None:
    """A member's copy of the room's tree: the public decomp at the host's starting commit (if it is one), then the
    room's own commits from the host. `get_bundle(have)` asks the host. The room's commits are followed as far as
    they are the work a room does (build_changes); with `trust` (the member trusts the host with this computer:
    building the tree runs its build setup) all of them. Returns the commits held back, as Link.held has them."""
    if not trust and not verifiable(info):
        raise NetworkError(UNVERIFIABLE)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q")
    have = ""
    if info.get("repo_url"):
        log(f"getting the project from {info['repo_url']}")
        _git(dest, "remote", "add", "origin", info["repo_url"])
        if info.get("base"):
            got = subprocess.run(["git", "-c", "safe.directory=*", "fetch", "-q", "--depth", "1", "origin", info["base"]],
                                 cwd=dest, capture_output=True, text=True).returncode == 0
            if not got:  # a host that doesn't serve commits by id: its history, until the commit is there
                _git(dest, "fetch", "-q", "origin", timeout=3600)
            have = info["base"]
            _git(dest, "checkout", "-q", have)
        else:
            _git(dest, "fetch", "-q", "--depth", "1", "origin")
    log("getting the room's work from the host")
    data = get_bundle(have)
    held = None
    if data:
        tip = fetch_bundle(dest, data, info["branch"])
        held = None if trust else hold(dest, have, tip)
        if held:
            log(f"the room's commits change the project's build setup ({', '.join(held['paths'][:4])}"
                f"{', ...' if len(held['paths']) > 4 else ''}): you get the public project as it is, and the room's "
                "commits once you choose to follow them (the Team tab shows what they change)")
        _git(dest, "checkout", "-q", "-B", info["branch"], have if held else tip)
    elif have:
        _git(dest, "checkout", "-q", "-B", info["branch"], have)
    if (dest / ".gitmodules").exists():
        _git(dest, "submodule", "update", "--init", "--depth", "1", timeout=3600)
    return held


UNVERIFIABLE = ("this room's project comes whole from the host, its build setup too (configure.py, the build's "
                "scripts and tools), and building it runs that on this computer: join only a host you trust, and say "
                "so (in the app: \"I trust the host\"; decomp join: --trust-host)")


def follow(root: Path, tip: str) -> None:
    """Move the member's tree to the host's head `tip` (fetched with fetch_bundle). Work in progress in files (an
    agent's latest attempt) is kept unless the host changed the same lines; the member's own commits are dropped: the
    host has them (it confirmed and committed every match the member sent), or they didn't hold up there."""
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    if dirty:
        _git(root, "stash", "push", "-q", "-m", "decomp.wtf: work in progress before following the room")
    _git(root, "reset", "-q", "--hard", tip)
    if dirty and subprocess.run(["git", "-c", "safe.directory=*", "stash", "pop", "-q"], cwd=root,
                                capture_output=True).returncode != 0:
        _git(root, "reset", "-q", "--hard", tip)  # the host changed the same lines: the host's version stays
        _git(root, "stash", "drop", "-q", check=False)


# What a room's commits change when they're its work (the host makes them for matches, edits, splits and links):
# C/C++ sources and headers, the lists dtk and ds-decomp read in config/ (splits, delinks, symbols: data, never run),
# and configure.py's Object(...) entries. Anything else may run when the
# tree is built (configure.py itself, the build's scripts and tools, its submodules), so a member follows it only once
# they choose to. Stricter than what agents may edit (ForkProject.safe_path): the host's own commits count too.
_ROOM_PARTS = FORBIDDEN_PARTS - {"config"}
_SPLITS = re.compile(r"^config/(.+/)?(splits|delinks|symbols)\.txt$")
_STATE = re.compile(r'(NonMatching|Matching|Equivalent|MatchingFor\(\s*"[\w.-]+"(\s*,\s*"[\w.-]+")*\s*\))')
_SOURCE = re.compile(r"^[\w./+-]+$")
MAX_DIFF = 60_000


def build_changes(root: Path, old: str, new: str) -> list[str]:
    """The files the commits from `old` to `new` change beyond the work a room does (see above)."""
    out = []
    fields = _git(root, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", old, new).split("\0")
    for meta, path in zip(fields[::2], fields[1::2]):
        if not meta.startswith(":"):
            break
        modes = set(meta[1:].split()[:2])
        parts = Path(path).parts
        if not modes <= {"000000", "100644", "100755"} or set(parts) & _ROOM_PARTS:  # symlinks, submodules, tools...
            out.append(path)
        elif EDITABLE.search(parts[-1]) and parts[0] != "config" or _SPLITS.match(path):
            continue
        elif path != "configure.py" or not _only_objects(_show(root, old, path), _show(root, new, path)):
            out.append(path)
    return out


def hold(root: Path, old: str, new: str) -> dict | None:
    """The room's commits from `old` to `new`, held back when they change more than a room's work: {"head", "paths",
    "diff" (of those files, to look at before following)}. None: nothing to hold back."""
    paths = build_changes(root, old, new)
    if not paths:
        return None
    diff = _git(root, "diff", "--no-ext-diff", "--no-color", old, new, "--", *paths, check=False)
    return {"head": new, "paths": paths, "diff": diff if len(diff) <= MAX_DIFF else diff[:MAX_DIFF] + "\n[...]"}


def _show(root: Path, rev: str, path: str) -> str:
    return _git(root, "show", f"{rev}:{path}", check=False)


def _only_objects(old: str, new: str) -> bool:
    """Whether configure.py changed only in its Object(...) entries, the way a room changes them: a file marked linked
    (Matching, MatchingFor("GALE01")...), or a new file listed with the settings an existing entry has."""
    def entries(text: str) -> list[tuple[str, str, str, int, int]]:
        out = []
        for m in dtksplits._OBJ_ENTRY.finditer(text):
            end = dtksplits._match(text, text.index("(", m.start()))
            tail = re.sub(r"\s+", " ", text[m.end():end - 1]).strip().rstrip(",").rstrip()
            out.append((re.sub(r"\s+", "", m.group(1)), m.group(2), tail, m.start(), end))
        return out

    def skeleton(text: str, found: list) -> str:  # the file without its entries (a run of entries counts as one)
        parts, last = [], 0
        for *_, start, end in found:
            parts += [text[last:start], "\0"]
            last = end
        return re.sub(r"\0(\s*,\s*\0)+", "\0", "".join(parts) + text[last:])

    before, after = entries(old), entries(new)
    if not before or skeleton(old, before) != skeleton(new, after):
        return False
    states = {e[0] for e in before}
    tails: dict[str, set[str]] = {}
    for state, source, tail, *_ in before:
        tails.setdefault(source, set()).add(tail)
    every_tail = {t for ts in tails.values() for t in ts}
    for state, source, tail, *_ in after:
        if not (_STATE.fullmatch(state) or state in states):
            return False
        if source in tails:
            if tail not in tails[source]:
                return False
        elif not _SOURCE.match(source) or ".." in source.split("/") or tail not in every_tail | {""}:
            return False
    return True


def register_member(net, slug: str, root: Path, info: dict, host_title: str) -> dict:
    """The project's settings on the member's side, as the host has them."""
    cfg = {"kind": "fork", "origin": info.get("origin") if info.get("origin") == "scratch" else None,
           "repo_url": info.get("repo_url"), "platform": info.get("platform") or "", "version": info.get("version"),
           "fork_kind": ForkProject(root).kind(), "ready": False, "compiler": info.get("compiler"),
           "contribution_policy": contribution_policy.inspect(root),
           "legal": {"basis": "personal-copy", "license": "personal use only",
                     "statement": "joined a room with the member's own copy of the game, confirmed legally obtained"}}
    if info.get("sha1"):
        cfg["program_sha1" if info.get("origin") == "scratch" else "game_sha1"] = info["sha1"]
    net.forks._register(slug, info["title"], f"{info['title']}, together with {host_title}", root, cfg,
                        f"joined {host_title}'s room")
    return cfg


def save_head(net, slug: str, tip: str) -> None:
    proj = net.project_row(slug)
    cfg = json.loads(proj["config"])
    if (cfg.get("link") or {}).get("head") != tip:
        cfg.setdefault("link", {})["head"] = tip
        with net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
