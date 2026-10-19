"""Private rooms: friends who each own the same game decompile it together.

The host's decomp.wtf is the hub. It serves a small, separate HTTP app (only /collab routes; never the GUI or the
agents' MCP endpoint) that a quick tunnel makes reachable. A friend joins with the invite link *and their own copy of
the game*: the ROM's SHA-1 must equal the host's, checked on both sides. No ROM bytes or assembly ever cross the
wire. Each side reads the code from its own copy; what travels is the work: claims (so nobody does the same function
twice), C attempts, matches, names, struct/prototype edits, notes. The host re-compiles every attempt it accepts
against its own ROM, so a match always means the same thing.

Host side: `host_app()`. Guest side: `Link`, which RomNetwork consults for every collaborative action.
Projects that are git source trees (GameCube, Wii, DS, forks of public decomps) work the same way, with the host's
tree as the room's truth: see forkroom.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .. import toolchains
from ..compiler import SourceRejected, check_includes
from ..network import Agent, NetworkError, _hash_token
from ..rom.symbols import AUTO_NAME
from . import forkroom, identity

NAME = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
MAX_BODY = 256 * 1024
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# A room anyone can reach (a public one, or an invite link that got around) meets strangers: how often people may
# try to join, from one connection and in all, within JOIN_WINDOW seconds; how many may wait for the host at once.
JOIN_WINDOW, JOINS_PER_ADDRESS, JOINS_PER_ROOM, MAX_WAITING = 600, 5, 30, 50
NONCE_TTL = 600  # how long a new member has to answer hello's challenge with their copy of the game


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def own_copy(net, slug: str) -> forkroom.Game | None:
    """This computer's copy of a project's game (the bytes its identity is the sha1 of), where the project keeps it:
    what a host checks new members' proofs against, and what a member proves with when they join again."""
    proj = net.project_row(slug)
    if net.fork_slug(slug):
        _, fp, cfg = net.forks.get(slug)
        return forkroom.own_copy(fp, cfg)
    rom = json.loads(proj["config"]).get("rom") or {}
    path = Path(proj["root"]) / (rom.get("program") or rom.get("file") or "")
    return forkroom.in_file(path) if rom and path.is_file() else None


def _answer(hello: dict, game: forkroom.Game | None) -> dict:
    """The proof of a copy of the game a join carries, for the host's challenge (forkroom.proof)."""
    if not hello.get("challenge"):
        return {}  # a host from before the check
    if game is None:
        raise NetworkError("decomp.wtf can't find your copy of the game in this project, and the room checks it: join "
                           "the room again with it (Join a team)")
    return {"challenge": hello["challenge"], "proof": forkroom.proof(hello["challenge"], game)}


# ---------------------------------------------------------------------------------------------- host

class Room:
    """Host-side state for one shared project."""

    def __init__(self, net, slug: str):
        self.net, self.slug = net, slug
        self.proj = net.project_row(slug)
        if net.cfg(self.proj).get("mapping"):
            raise NetworkError("mapping v1 supports local workers; room sharing needs an overlay-aware protocol")
        self.fork = bool(net.fork_slug(slug))  # a git source tree (forkroom.py)
        self._hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=900))
        self._members: tuple[float, list] = (0.0, [])  # cached: every member polls, the list changes slowly
        self._secret = secrets.token_bytes(32)  # signs hello's challenges, so anyone may ask for one and none is stored
        self._spent: dict[str, float] = {}  # challenges answered already (until they expire): each is good once
        self._joins: deque = deque(maxlen=JOINS_PER_ROOM)  # when people tried to join
        self._joins_from: dict[str, deque] = {}  # ...from each connection
        self.url = ""  # where members reach the room now (set when it's shared): what its key signs it's at
        self.published: dict | None = None  # where it said it is (identity.publish), for members after a restart
        self.publish_error = ""
        self.publish_after = 0.0  # after a failed publish, when to try again

    @property
    def settings(self) -> dict:
        """visibility: private (an invite link is needed) or public (the room's address is enough); approve: the
        host lets each new member in (by default in a public room); limit: at most this many members (0: any)."""
        c = self.cfg.get("collab") or {}
        visibility = c.get("visibility", "private")
        return {"visibility": visibility, "approve": bool(c.get("approve", visibility == "public")),
                "limit": int(c.get("limit") or 0), "findable": bool(c.get("findable", True))}

    def configure(self, visibility: str | None = None, approve: bool | None = None, limit: int | None = None,
                  findable: bool | None = None) -> dict:
        """findable: a room shared through a quick tunnel says where it is now (identity.py: in a secret gist on the
        host's GitHub account, once the host has connected GitHub), so members find it when the address changes."""
        cfg = self.cfg
        c = cfg.setdefault("collab", {})
        if visibility is not None:
            if visibility not in ("private", "public"):
                raise NetworkError("visibility is private or public")
            if visibility == "public" and c.get("visibility") != "public" and approve is None:
                c["approve"] = True  # strangers can reach a public room: they wait to be let in unless the host says so
            c["visibility"] = visibility
        if approve is not None:
            c["approve"] = bool(approve)
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise NetworkError("the member limit is a number (0: no limit)") from None
            if limit < 0:
                raise NetworkError("the member limit is a number (0: no limit)")
            c["limit"] = limit
        if findable is not None:
            c["findable"] = bool(findable)
            if not findable:
                self.published = None  # the note in the gist expires by itself (identity.POINTER_LIFE)
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return self.settings

    @property
    def secret(self) -> str:
        """The room's private key, made the first time it's shared and kept with the project (identity.py)."""
        c = self.cfg.get("collab") or {}
        if c.get("key"):
            return c["key"]
        with self.net.store.tx() as db:
            cfg = self.cfg
            c = cfg.setdefault("collab", {})
            c.setdefault("key", identity.new_secret())
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return c["key"]

    @property
    def key(self) -> str:
        """The room's public key: in its invite links, so members can tell it's the room they joined."""
        return identity.public_key(self.secret)

    @property
    def gist(self) -> str:
        """The host's gist with the note of where the room is now (identity.publish): members are told which."""
        return (self.cfg.get("collab") or {}).get("gist") or ""

    def _keep_gist(self, gist: str) -> None:
        with self.net.store.tx() as db:
            cfg = self.cfg
            cfg.setdefault("collab", {})["gist"] = gist
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))

    def link(self, base: str) -> str:
        """The room's link at the address `base`: its invite code (none for a public room), its name and its key."""
        code = "open" if self.settings["visibility"] == "public" else (self.cfg.get("collab") or {}).get("code", "")
        return f"{base.rstrip('/')}/join/{code}#room={self.slug}&key={self.key}"

    def challenge(self) -> str:
        """A one-time nonce for a new member's proof that they have the game (forkroom.proof), good for NONCE_TTL."""
        body = f"{int(time.time()) + NONCE_TTL}.{secrets.token_urlsafe(12)}"
        return f"{body}.{hmac.new(self._secret, body.encode(), 'sha256').hexdigest()[:32]}"

    def _spend(self, nonce: str) -> None:
        body, _, sig = nonce.rpartition(".")
        expires = body.split(".", 1)[0]
        now = time.time()
        if not (sig and hmac.compare_digest(sig, hmac.new(self._secret, body.encode(), "sha256").hexdigest()[:32])
                and expires.isdigit() and int(expires) > now and nonce not in self._spent):
            raise NetworkError("the check of your copy of the game expired: join again")
        self._spent = {n: t for n, t in self._spent.items() if t > now}
        self._spent[nonce] = int(expires)

    def check_copy(self, nonce: str, proof: str) -> None:
        """A new member proves they have the game: their answer to the challenge must be what this computer's copy
        answers. A room whose copy isn't where the project keeps it can't check: only its invite link lets people in."""
        copy = own_copy(self.net, self.slug)
        if copy is None:
            if self.settings["visibility"] == "public":
                raise NetworkError("the host's copy of the game isn't where its project keeps it, so this public room "
                                   "can't check that new members have the game: ask the host for an invite link")
            return
        if not proof:
            raise NetworkError("this room checks every new member's copy of the game: update decomp.wtf, then join again")
        self._spend(nonce)
        if not hmac.compare_digest(str(proof), forkroom.proof(nonce, copy)):
            raise NetworkError("that isn't the same game: everyone in a room needs their own copy of exactly this game")

    def throttle(self, address: str) -> None:
        """Count an attempt to join, refusing it when one connection, or everyone, tried too often lately."""
        now = time.time()
        if len(self._joins_from) > 1000:  # forget connections that stopped trying
            self._joins_from = {a: q for a, q in self._joins_from.items() if now - q[-1] < JOIN_WINDOW}
        mine = self._joins_from.setdefault(address, deque(maxlen=JOINS_PER_ADDRESS))
        if len(mine) == mine.maxlen and now - mine[0] < JOIN_WINDOW:
            raise NetworkError("too many tries to join from your connection: wait a few minutes, then try again")
        if len(self._joins) == self._joins.maxlen and now - self._joins[0] < JOIN_WINDOW:
            raise NetworkError("many people are trying to join this room right now: wait a few minutes, then try again")
        mine.append(now)
        self._joins.append(now)

    def _full(self) -> bool:
        limit = self.settings["limit"]
        return bool(limit) and self.net.store.one("SELECT COUNT(*) AS n FROM agents WHERE origin=? AND owner IS NULL",
                                                  self.origin)["n"] >= limit

    def pending(self) -> list[dict]:
        return [dict(r) for r in self.net.store.all(
            "SELECT name, created_at FROM agents WHERE origin=? AND owner IS NULL ORDER BY created_at", self.origin + ":pending")]

    def approve(self, name: str, ok: bool = True) -> None:
        if ok and self._full():
            raise NetworkError(f"the room is full ({self.settings['limit']} members): raise its limit first")
        with self.net.store.tx() as db:
            row = db.execute("SELECT id FROM agents WHERE name=? AND origin=?", (name, self.origin + ":pending")).fetchone()
            if row is None:
                raise NetworkError(f"nobody called {name} is waiting")
            if ok:
                db.execute("UPDATE agents SET origin=? WHERE id=?", (self.origin, row["id"]))
                self.net._event(db, self.proj["id"], None, "collab", f"{name} joined")
            else:
                db.execute("UPDATE agents SET origin=?, token_hash=? WHERE id=?",
                           (self.origin + ":declined", "declined:" + secrets.token_hex(16), row["id"]))
        self._members = (0.0, [])

    @property
    def cfg(self) -> dict:
        return json.loads(self.net.project_row(self.slug)["config"])

    def open_invite(self, keep: bool = False, **where) -> str:
        """Open the room and return its invite code: a new one, or with `keep` the current one (the room reopening
        after the app restarted, so links people already have keep working). `where`: how it's shared
        (mode="tunnel" or mode="address" with address and port), remembered for reopening."""
        cfg = self.cfg
        old = cfg.get("collab") or {}
        code = old.get("code") if keep and old.get("code") else secrets.token_urlsafe(16)
        cfg["collab"] = {**old, **where, "invite": _hash(code), "code": code, "open": True,
                         "since": old.get("since") or time.time()}
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return code

    def remove(self, name: str) -> None:
        """Take a member out of the room: their token stops working and their claims are freed."""
        with self.net.store.tx() as db:
            row = db.execute("SELECT id FROM agents WHERE name=? AND origin=? AND owner IS NULL", (name, self.origin)).fetchone()
            if row is None:
                raise NetworkError(f"{name} isn't a member of this room")
            db.execute("UPDATE agents SET token_hash=? WHERE id=?", ("removed:" + secrets.token_hex(16), row["id"]))
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE owner=? AND origin=?)",
                       (name, self.origin))
            db.execute("UPDATE agents SET origin=? WHERE id=?", (self.origin + ":removed", row["id"]))
            self.net._event(db, self.proj["id"], None, "collab", f"{name} was removed from the room")
        self._members = (0.0, [])

    def close(self) -> None:
        cfg = self.cfg
        if "collab" in cfg:
            cfg["collab"]["open"] = False
            with self.net.store.tx() as db:
                db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))

    def invite_ok(self, code: str) -> bool:
        c = self.cfg.get("collab") or {}
        if not c.get("open"):
            return False
        return c.get("visibility") == "public" or secrets.compare_digest(c.get("invite", ""), _hash(code or ""))

    @property
    def origin(self) -> str:
        return f"collab:{self.slug}"

    def members(self) -> list[dict]:
        at, cached = self._members
        if time.time() - at < 3:
            return cached
        out = self._members_now()
        self._members = (time.time(), out)
        return out

    def _members_now(self) -> list[dict]:
        rows = self.net.store.all("SELECT id, name, created_at, last_seen FROM agents WHERE origin=? AND owner IS NULL",
                                  self.origin)
        out = []
        for r in rows:
            agents = self.net.store.all("SELECT id, name FROM agents WHERE origin=? AND owner=?", self.origin, r["name"])
            ids = [a["id"] for a in agents] or [-1]
            matched = self.net.store.one(
                f"SELECT COUNT(*) AS n FROM functions WHERE matched_agent_id IN ({','.join('?' * len(ids))})", *ids)["n"]
            working = [c["name"] for c in self.net.store.all(
                f"SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id IN "
                f"({','.join('?' * len(ids))})", *ids)]
            out.append({"name": r["name"], "joined": r["created_at"], "last_seen": r["last_seen"],
                        "online": bool(r["last_seen"]) and time.time() - r["last_seen"] < 30,
                        "agents": [a["name"].split(".", 1)[1] for a in agents], "matched": matched, "working": working})
        return out

    def join(self, name: str, sha1: str, nonce: str = "", proof: str = "") -> tuple[str, str]:
        """New member: (token, the name they got; a taken name gets a number). `proof` answers the challenge `nonce`
        hello gave them, with their copy of the game (check_copy)."""
        if not NAME.match(name or ""):
            raise NetworkError("pick a name of up to 24 letters, digits, - or _")
        cfg = self.cfg
        want = forkroom.project_identity(cfg)
        if want and sha1 != want:
            what = (f"{cfg['rom']['title']}, {cfg['rom']['game_code']}" if not self.fork
                    else f"{self.proj['name']}, {cfg.get('version') or 'the same version'}")
            raise NetworkError("that isn't the same game: everyone in a room needs their own copy of exactly this "
                               f"game ({what}, same version)")
        self.check_copy(nonce, proof)
        if self._full():
            raise NetworkError(f"this room is full ({self.settings['limit']} members): ask the host to make room")
        if self.settings["approve"] and len(self.pending()) >= MAX_WAITING:
            raise NetworkError("many people are waiting for the host to let them in already: try again later")
        base, n = name, 2
        while self.net.store.one("SELECT 1 FROM agents WHERE lower(name)=lower(?)", name):
            name, n = f"{base}{n}", n + 1
        token = "dn_" + secrets.token_urlsafe(24)
        waiting = self.settings["approve"]
        with self.net.store.tx() as db:
            db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                       (name, None, _hash_token(token), time.time(), self.origin + (":pending" if waiting else "")))
            if waiting:
                self.net._event(db, self.proj["id"], None, "collab", f"{name} asks to join")
            else:
                self.net._event(db, self.proj["id"], None, "collab", f"{name} joined")
        self._members = (0.0, [])
        return token, name

    def member(self, request: Request) -> Agent:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        agent = self.net.authenticate(token or None)
        row = self.net.store.one("SELECT origin, owner FROM agents WHERE id=?", agent.id)
        if row["origin"] == self.origin + ":pending":
            raise NetworkError("waiting for the host to let you in")
        if row["origin"] != self.origin or row["owner"] is not None:
            raise NetworkError("not a member of this room")
        hits = self._hits[agent.name]
        now = time.time()
        hits.append(now)
        if len(hits) == hits.maxlen and now - hits[0] < 60:
            raise NetworkError("slow down")
        return agent

    def agent_for(self, member: Agent, label: str) -> Agent:
        """The host-side identity of one of a member's agents, e.g. alex.claude-1."""
        label = re.sub(r"[^A-Za-z0-9_-]", "", str(label or "agent"))[:24] or "agent"
        name = f"{member.name}.{label}"
        row = self.net.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is None:
            with self.net.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (name, member.name, "remote:" + secrets.token_hex(16), time.time(), self.origin)).lastrowid
            return Agent(aid, name, member.name, True)
        return Agent(row["id"], name, member.name, True)

    def state(self, e: int, a: int, ctx_version: int, member: Agent) -> dict:
        """What changed since the guest's cursors: work product only, never ROM-derived data."""
        if self.fork:
            return self._fork_state(e, member)
        pid = self.proj["id"]
        net = self.net
        with net.store.tx() as db:
            net._expire_claims(db)
        events = net.store.all("SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
                               "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id LIMIT 500", pid, e)
        max_e = events[-1]["id"] if events else e
        kinds = {ev["kind"] for ev in events}
        names = {ev["detail"] for ev in events if ev["kind"] in ("match",)}
        out: dict = {"e": max_e, "events": [dict(ev) for ev in events]}
        cfg = self.cfg
        if cfg.get("platform") in ("sms", "gg") and cfg.get("sms_profile") is not None:
            out["sms_profile"] = cfg["sms_profile"]
        first = e == 0
        if first or names:
            rows = net.store.all("SELECT f.name, f.vram, f.rom_offset, f.size, f.matched_source, a.name AS agent "
                                 "FROM functions f LEFT JOIN agents a "
                                 "ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='matched'", pid)
            out["matched"] = [dict(r) for r in rows if first or r["name"] in names]
        best = net.store.all("SELECT f.name, f.vram, f.rom_offset, f.size, f.best_score, f.best_source, MAX(t.id) AS last "
                             "FROM attempts t "
                             "JOIN functions f ON f.id=t.function_id WHERE f.project_id=? AND t.id>? AND f.status='open' "
                             "AND f.best_source IS NOT NULL GROUP BY f.id", pid, a)
        out["best"] = [{"name": r["name"], "vram": r["vram"], "rom_offset": r["rom_offset"], "size": r["size"],
                        "score": r["best_score"], "source": r["best_source"]} for r in best]
        last = net.store.one("SELECT MAX(t.id) AS m FROM attempts t JOIN functions f ON f.id=t.function_id WHERE f.project_id=?", pid)
        out["a"] = max(a, last["m"] or 0)
        if first or "documented" in kinds:  # routines documented in assembly (the Game Boy's assembly track): the new ones
            since = min((ev["created_at"] for ev in events if ev["kind"] == "documented"), default=0) - 60  # (its row was saved just before)
            out["documented"] = [dict(r) for r in net.store.all(
                "SELECT f.name, f.vram, f.rom_offset, f.size, f.matched_source AS source, a.name AS agent FROM functions f "
                "LEFT JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='documented' "
                "AND (? OR f.matched_at >= ?)", pid, first, since)]
        if first or kinds & {"rename", "split", "merge", "documented"}:  # (documenting names a routine and what it uses)
            out["symbols"] = [dict(r) for r in net.store.all(
                "SELECT name, address, size, kind FROM symbols WHERE project_id=?", pid) if not AUTO_NAME.match(r["name"])]
            out["functions"] = [dict(r) for r in net.store.all(
                "SELECT name, vram, size, rom_offset FROM functions WHERE project_id=? ORDER BY vram", pid)]
        ctx = net._context(pid)
        if ctx["version"] != ctx_version:
            out["context"] = {"version": ctx["version"], "header": ctx["header"]}
        if first or kinds & {"note"}:
            out["notes"] = [dict(r) for r in net.store.all(
                "SELECT n.id, n.text, n.created_at, f.vram, f.name AS function, f.rom_offset, a.name AS agent FROM notes n "
                "LEFT JOIN functions f ON f.id=n.function_id "
                "LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id", pid)]
        out["claims"] = [dict(r) for r in net.store.all(
            "SELECT f.name, f.vram, a.name AS agent FROM claims c JOIN functions f ON f.id=c.function_id JOIN agents a "
            "ON a.id=c.agent_id WHERE f.project_id=?", pid)]
        out["members"] = self.members()
        return out

    def _fork_state(self, e: int, member: Agent) -> dict:
        """A git project's room: events, matches (by name), claims and notes; the tree itself travels as bundles
        (GET /collab/bundle) when `head` moves."""
        pid, net = self.proj["id"], self.net
        with net.store.tx() as db:
            net._expire_claims(db)
        events = net.store.all("SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
                               "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id LIMIT 500", pid, e)
        out: dict = {"e": events[-1]["id"] if events else e, "events": [dict(ev) for ev in events]}
        names = {ev["detail"] for ev in events if ev["kind"] == "match"}
        if e == 0 or names:
            rows = net.store.all("SELECT f.name, a.name AS agent FROM functions f LEFT JOIN agents a ON a.id=f.matched_agent_id "
                                 "WHERE f.project_id=? AND f.status='matched'", pid)
            out["matched"] = [dict(r) for r in rows if e == 0 or r["name"] in names]
        if e == 0 or any(ev["kind"] == "note" for ev in events):
            out["notes"] = [dict(r) for r in net.store.all(
                "SELECT n.id, n.text, n.created_at, f.name AS function, a.name AS agent FROM notes n LEFT JOIN functions f "
                "ON f.id=n.function_id LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id", pid)]
        out["claims"] = [dict(r) for r in net.store.all(
            "SELECT f.name, f.vram, a.name AS agent FROM claims c JOIN functions f ON f.id=c.function_id JOIN agents a "
            "ON a.id=c.agent_id WHERE f.project_id=?", pid)]
        out["head"] = forkroom.head(Path(self.proj["root"]))
        out["branch"] = forkroom.branch(Path(self.proj["root"]))
        out["members"] = self.members()
        return out


def host_app(net, rooms: dict[str, Room], tunnel: bool = False) -> Starlette:
    """The public side of a shared project, served through the tunnel (`tunnel`: on this computer's loopback, where
    only the quick tunnel reaches it) or at an address of the host's own."""

    def client(request: Request) -> str:
        """Which connection asks, to count its tries to join: behind the tunnel, Cloudflare says (every request comes
        from cloudflared on this computer); at the host's own address, the connection itself does."""
        peer = request.client.host if request.client else ""
        if tunnel and peer in ("127.0.0.1", "::1"):
            return request.headers.get("cf-connecting-ip", "").strip() or "tunnel"
        return peer or "unknown"

    def room_for(request: Request) -> Room:
        slug = request.query_params.get("room") or request.headers.get("x-room", "")
        if not slug and len(rooms) == 1:  # a public room's bare address
            slug = next(iter(rooms))
        room = rooms.get(slug)
        if room is None:
            raise NetworkError("this room is closed")
        return room

    async def body(request: Request) -> dict:
        # read before anyone is known to be a member, and the host is shared at a public address
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            raise NetworkError("invalid request") from None
        if declared > MAX_BODY:
            raise NetworkError("request too large")
        raw = bytearray()
        async for chunk in request.stream():  # a chunked request has no length to check: count as it comes
            raw += chunk
            if len(raw) > MAX_BODY:
                raise NetworkError("request too large")
        try:
            data = json.loads(raw)
        except ValueError:
            raise NetworkError("invalid request") from None
        if not isinstance(data, dict):
            raise NetworkError("invalid request")
        return data

    def endpoint(fn, auth: bool = True):
        async def handler(request: Request):
            import anyio

            try:
                room = room_for(request)
                data = await body(request) if request.method == "POST" else dict(request.query_params)
                member = await anyio.to_thread.run_sync(room.member, request) if auth else None
                if not auth:
                    data["_from"] = client(request)
                    data["_base"] = f"{request.url.scheme}://{request.headers.get('host', '')}"
                result = await anyio.to_thread.run_sync(lambda: fn(room, member, data))
                return JSONResponse(result)
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return handler

    def hello(room, member, q):
        if not room.invite_ok(q.get("invite", "")):
            raise NetworkError("this invite link is no longer valid")
        cfg = room.cfg
        ov = net.overview(room.slug)
        common = {"functions": ov["functions"], "matched": ov["matched"], "members": len(room.members()),
                  "challenge": room.challenge(), **room.settings}
        if room.fork:
            return {"room": room.slug, **forkroom.hello(net, room.slug), **common}
        return {"room": room.slug, "kind": "rom", "title": cfg["rom"]["title"], "game_code": cfg["rom"]["game_code"],
                "region": cfg["rom"]["region"], "sha1": cfg["rom"]["sha1"], "compiler": cfg["compiler"],
                "platform": cfg.get("platform") or "", **common}

    def ping(room, member, q):
        """The room shows it holds its key: a signature over where it is and the asker's nonce (identity.identify).
        Needs no invite: a member whose host moved checks the new address with it, and so does the host's tunnel
        keeper (Keeper)."""
        nonce, url = str(q.get("nonce", ""))[:64], room.url or q["_base"]
        return {"room": room.slug, "key": room.key,
                "signature": identity.sign(room.secret, identity.ping_message(url.rstrip("/"), room.slug, nonce))}

    def join(room, member, d):
        if not room.invite_ok(str(d.get("invite", ""))):
            raise NetworkError("this invite link is no longer valid")
        room.throttle(d["_from"])
        token, name = room.join(str(d.get("name", "")), str(d.get("sha1", "")), str(d.get("challenge", "")),
                                str(d.get("proof", "")))
        return {"token": token, "room": room.slug, "name": name, "waiting": room.settings["approve"]}

    def whoami(room, member, q):
        return {"name": member.name, "room": room.slug}

    def state(room, member, q):
        # with the gist that says where the room is (for members only: it names the host's GitHub account)
        return {**room.state(int(q.get("e") or 0), int(q.get("a") or 0), int(q.get("v") or 0), member),
                "gist": room.gist if room.settings["findable"] else ""}

    def claim(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        if d.get("track") == "asm":  # a hand-written Game Boy routine to document
            fn = net.claim_asm_row(agent, room.slug, d.get("function") or None)
        else:
            fn = net.claim_row(agent, room.slug, d.get("function") or None, str(d.get("strategy") or "easiest"))
        return {"function": fn["name"] if fn else None, "vram": fn["vram"] if fn else None}

    def document(room, member, d):
        """A member's documented routine: judged again here (this copy is the room's), and kept when it passes."""
        agent = room.agent_for(member, d.get("agent"))
        name = str(d.get("function", ""))
        if isinstance(d.get("rom_offset"), int):  # (by its bytes: documenting renames, so names part ways for a moment)
            row = net._room_function(room.proj["id"], name, None, d["rom_offset"], d.get("size"))
            name = row["name"] if row is not None else name
        return {"text": net.document_function(agent, room.slug, name, str(d.get("source", ""))[:65536])}

    def attempt(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        res = net.attempt(agent, str(d.get("function", "")), str(d.get("source", "")), room.slug)
        # byte-identical here confirms a member's match, also when the room has it already (someone else's, or the
        # member's own best, which the host compiled and found exact): the member keeps it, then syncs the room's
        return {"score": res["score"], "matched": res["matched"] or res["score"] == 0}

    def best(room, member, d):
        """A member's attempt got better: kept as the function's best so far as this copy scores it (compiled here
        only when it claims to beat the best)."""
        agent = room.agent_for(member, d.get("agent"))
        net.apply_reported_best(room.slug, str(d.get("function", "")), int(d.get("score", 0)), str(d.get("source", ""))[:65536], agent)
        return {"ok": True}

    def release(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.release(agent, str(d.get("function", "")), str(d.get("notes", "")), room.slug)}

    def context(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.edit_context(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                         str(d.get("message", "")), d.get("function") or None)}

    def rename(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.rename_symbol(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                          str(d.get("message", "")))}

    def split(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.split_function(agent, room.slug, str(d.get("function", "")), str(d.get("at", "")),
                                           str(d.get("new_name", "")))}

    def merge(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.merge_function(agent, room.slug, str(d.get("function", "")))}

    def note(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.add_note(agent, room.slug, str(d.get("text", "")), d.get("function") or None)}

    def edit(room, member, d):
        """A member's edit of a git project's file (a header, a new or shared source file): made and checked here."""
        if not room.fork:
            raise NetworkError("this room's project has no files to edit")
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.forks.edit_file(agent, room.slug, str(d.get("path", "")), str(d.get("old", "")),
                                            str(d.get("new", "")), str(d.get("message", "")))}

    def split_file(room, member, d):
        if not room.fork:
            raise NetworkError("this room's project has no files to split")
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.forks.split_file(agent, room.slug, str(d.get("path", "")), str(d.get("at", "")),
                                             str(d.get("new_name", "")), str(d.get("data", "")))}

    async def get_bundle(request: Request):
        """The host's commits a member doesn't have yet (a git bundle: tracked files only, never the game)."""
        import anyio

        try:
            room = room_for(request)
            await anyio.to_thread.run_sync(room.member, request)
            if not room.fork:
                raise NetworkError("this room's project isn't a git tree")
            data = await anyio.to_thread.run_sync(lambda: forkroom.bundle(Path(room.proj["root"]),
                                                                           request.query_params.get("have", "")))
        except NetworkError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(data, media_type="application/octet-stream", status_code=200 if data else 204)

    def leave(room, member, d):
        with net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE owner=? AND origin=?)",
                       (member.name, room.origin))
        return {"ok": True}

    async def landing(request: Request):
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>decomp.wtf room</title>"
                            "<body style='font:16px system-ui;max-width:36em;margin:4rem auto;padding:0 1rem'>"
                            "<h1>decomp.wtf room</h1><p>This is an invite to decompile a game together. Open "
                            "<b>decomp.wtf</b>, choose <b>Join a friend</b>, and paste this page's address. "
                            "You'll need your own copy of the game.</p><p>Don't have the app? "
                            "<a href='https://decomp.wtf'>decomp.wtf</a></p>")

    routes = [
        Route("/", landing), Route("/join/{code}", landing),
        Route("/collab/hello", endpoint(hello, auth=False)),
        Route("/collab/ping", endpoint(ping, auth=False)),
        Route("/collab/join", endpoint(join, auth=False), methods=["POST"]),
        Route("/collab/state", endpoint(state)),
        Route("/collab/whoami", endpoint(whoami)),
        Route("/collab/claim", endpoint(claim), methods=["POST"]),
        Route("/collab/attempt", endpoint(attempt), methods=["POST"]),
        Route("/collab/document", endpoint(document), methods=["POST"]),
        Route("/collab/best", endpoint(best), methods=["POST"]),
        Route("/collab/release", endpoint(release), methods=["POST"]),
        Route("/collab/context", endpoint(context), methods=["POST"]),
        Route("/collab/rename", endpoint(rename), methods=["POST"]),
        Route("/collab/split", endpoint(split), methods=["POST"]),
        Route("/collab/merge", endpoint(merge), methods=["POST"]),
        Route("/collab/note", endpoint(note), methods=["POST"]),
        Route("/collab/leave", endpoint(leave), methods=["POST"]),
        Route("/collab/edit", endpoint(edit), methods=["POST"]),
        Route("/collab/split_file", endpoint(split_file), methods=["POST"]),
        Route("/collab/bundle", get_bundle),
    ]
    return Starlette(routes=routes)


class Tunnel:
    """A Cloudflare quick tunnel (free, no account) to the collab server."""

    def __init__(self, port: int):
        self.port, self.proc, self.url = port, None, None
        self.started = 0.0

    def start(self, log=print, timeout: float = 45) -> str:
        if toolchains.installed("cloudflared") is None:
            toolchains.install("cloudflared", log=log)
        exe = toolchains.path("cloudflared") / ("cloudflared.exe" if (toolchains.path("cloudflared") / "cloudflared.exe").exists() else "cloudflared")
        self.proc = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace")
        found = threading.Event()

        def read():
            for line in self.proc.stderr:
                m = TUNNEL_URL.search(line)
                if m and not self.url:
                    self.url = m.group(0)
                    found.set()
        threading.Thread(target=read, daemon=True).start()
        if not found.wait(timeout):
            self.stop()
            raise RuntimeError("couldn't open a tunnel (is the internet reachable?)")
        log(f"tunnel open: {self.url}")
        self.started = time.time()
        return self.url

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and bool(self.url)

    def reachable(self, room: str, http: httpx.Client | None = None) -> bool:
        """Whether the room answers at the tunnel's public address: the tunnel's one connection can drop while
        cloudflared still runs."""
        try:
            r = (http or httpx).get(f"{self.url}/collab/ping", params={"room": room, "nonce": "keeper"}, timeout=15)
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc, self.url = None, None


class Keeper:
    """Keeps the rooms shared through the quick tunnel reachable. A quick tunnel promises no uptime: when cloudflared
    stops, or its address stops answering, the keeper opens a new one and puts its address in the rooms' links. And
    a room that's findable says where it is now (identity.publish: in the host's gist, once the host has connected
    GitHub), again whenever that changes or the note gets old, so members find it after the address changes. tick() runs every half minute, and at once after wake.set()
    (a room opened). `share` is the app's {"servers", "tunnel", "links"} (server.py)."""

    CHECK_EVERY, RECHECK, GRACE, FAILS = 300, 30, 90, 3  # seconds between checks (after a failed one), first check

    def __init__(self, net, share: dict, log=print, http: httpx.Client | None = None,
                 github: httpx.Client | None = None, token=lambda: None, tunnel=Tunnel):
        """`token()`: the host's GitHub token (a classic one with only the gist scope), or None: not connected."""
        self.net, self.share, self.log, self.http, self.tunnel = net, share, log, http, tunnel
        self.github, self.token = github, token
        self.fails, self.checked, self.backoff, self.retry_at = 0, 0.0, 30.0, 0.0
        self.lock = threading.Lock()  # one tunnel at a time: held while one opens (here, or in server.open_room)
        self.wake = threading.Event()
        self.stopped = False  # the app is quitting

    def rooms(self) -> list[Room]:
        return [r for r in list(self.net.rooms.values()) if (r.cfg.get("collab") or {}).get("mode", "tunnel") == "tunnel"]

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rooms = self.rooms()
        if not rooms or self.stopped:
            return
        with self.lock:
            t = self.share.get("tunnel")
            if t is None or not t.alive() or self.fails >= self.FAILS:
                self._reopen(rooms, now)
            elif now - t.started >= self.GRACE and now - self.checked >= (self.RECHECK if self.fails else self.CHECK_EVERY):
                self.checked = now  # (a new address takes a little while to resolve everywhere: GRACE)
                self.fails = 0 if t.reachable(rooms[0].slug, self.http) else self.fails + 1
        for room in rooms:
            self._publish(room, now)

    def _reopen(self, rooms: list[Room], now: float) -> None:
        server = self.share["servers"].get(("127.0.0.1", None))
        if server is None or now < self.retry_at:
            return
        old = self.share.get("tunnel")
        if old is not None:
            old.stop()
        t = self.tunnel(server[1])
        try:
            t.start(self.log)
        except (RuntimeError, OSError) as e:
            self.share["tunnel"] = None
            self.retry_at, self.backoff = now + self.backoff, min(self.backoff * 2, 600)
            self.log(f"the rooms' quick tunnel didn't open ({e}); trying again in a while")
            return
        self.share["tunnel"], self.fails, self.checked, self.backoff, self.retry_at = t, 0, now, 30.0, 0.0
        for room in rooms:
            room.url = t.url
            self.share["links"][room.slug] = room.link(t.url)
            with self.net.store.tx() as db:
                self.net._event(db, room.proj["id"], None, "collab", f"the room's quick tunnel opened at {t.url}")

    def _publish(self, room: Room, now: float) -> None:
        token = self.token()
        if not (token and room.settings["findable"] and identity.QUICK_TUNNEL.match(room.url or "")) \
                or now < room.publish_after:
            return
        note = room.published
        if note and identity.same_url(note["url"], room.url) and note["expires"] - now > identity.REFRESH_LEFT:
            return
        try:
            gist, room.published = identity.publish(room.secret, room.url, token, room.gist, self.github, now)
        except (httpx.HTTPError, ValueError) as e:
            room.publish_error, room.publish_after = str(e) or type(e).__name__, now + 600
            return
        room.publish_error = ""
        if gist != room.gist:
            room._keep_gist(gist)


# --------------------------------------------------------------------------------------------- guest

def parse_invite(link: str) -> tuple[str, str]:
    """(base url, invite code) from a link like https://abc.trycloudflare.com/join/CODE#room=slug&key=KEY"""
    link = link.strip().rstrip("/")
    if re.match(r"^https?://[^/\s]+$", link):  # a public room's bare address
        return link, "open"
    m = re.match(r"^(https?://[^/\s]+(?::\d+)?)/join/([A-Za-z0-9_-]{4,})(?:#room=([\w-]+)(?:&key=[A-Za-z0-9_-]{43})?)?$", link)
    if not m:
        raise NetworkError("that doesn't look like a decomp.wtf invite link")
    return m.group(1), m.group(2) + (f"#{m.group(3)}" if m.group(3) else "")


def _fragment(link: str) -> dict:
    """The room's name and key in a link (#room=slug&key=KEY)."""
    import urllib.parse

    return dict(urllib.parse.parse_qsl(link.split("#", 1)[1])) if "#" in link else {}


class Link:
    """The guest's connection to a host's room."""

    def __init__(self, net, slug: str, url: str, room: str, token: str, member: str, client: httpx.Client | None = None):
        self.net, self.slug, self.url, self.room, self.token, self.member = net, slug, url.rstrip("/"), room, token, member
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
        self.cursor = {"e": 0, "a": 0, "v": 0}
        self.claims: list[dict] = []
        self.members: list[dict] = []
        self.error = ""
        self.last_ok = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.fork = bool(net.fork_slug(slug))  # a git source tree: follows the host's commits (forkroom.py)
        info = json.loads(net.project_row(slug)["config"]).get("link") or {}
        self.head = info.get("head", "")
        self.trusted = info.get("trusted", "")  # the host's latest commit the member chose to follow (follow_held)
        self.held: dict | None = None  # the host's commits that change the build setup, until the member follows them
        self.key = info.get("key") or ""  # the room's key, as its host showed it when this member joined
        self.gist = info.get("gist") or ""  # the host's gist that says where the room is now (identity.py)
        self.github: httpx.Client | None = None  # for reading it (GitHub's API, no account); tests swap it
        self._looked = 0.0

    # -- transport

    def _req(self, method: str, path: str, data: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}", "x-room": self.room}
        try:
            if method == "GET":
                r = self.client.get(f"{self.url}{path}", params={**(data or {}), "room": self.room}, headers=headers)
            else:
                r = self.client.post(f"{self.url}{path}?room={self.room}", json=data or {}, headers=headers)
        except httpx.HTTPError as e:
            self.error = f"can't reach the host ({type(e).__name__})"
            raise NetworkError(f"the room's host isn't reachable right now: {self.error}") from None
        out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            raise NetworkError(out.get("error") or f"host answered {r.status_code}")
        self.error, self.last_ok = "", time.time()
        return out

    def _raw(self, path: str, params: dict) -> bytes:
        headers = {"authorization": f"Bearer {self.token}", "x-room": self.room}
        try:
            r = self.client.get(f"{self.url}{path}", params={**params, "room": self.room}, headers=headers,
                                timeout=httpx.Timeout(600, connect=15))
        except httpx.HTTPError as e:
            self.error = f"can't reach the host ({type(e).__name__})"
            raise NetworkError(f"the room's host isn't reachable right now: {self.error}") from None
        if r.status_code == 204:
            return b""
        if r.status_code != 200:
            raise NetworkError((r.json() if r.headers.get("content-type", "").startswith("application/json")
                                else {}).get("error") or f"host answered {r.status_code}")
        self.error, self.last_ok = "", time.time()
        return r.content

    # -- actions, called by RomNetwork for a linked project

    def claim(self, agent: Agent, function: str | None, strategy: str, track: str = "") -> str | None:
        return self._req("POST", "/collab/claim", {"agent": agent.name, "function": function, "strategy": strategy,
                                                   **({"track": track} if track else {})})["function"]

    def attempt(self, agent: Agent, function: str, source: str) -> dict:
        return self._req("POST", "/collab/attempt", {"agent": agent.name, "function": function, "source": source})

    def call(self, route: str, agent: Agent, /, **data) -> str:
        return self._req("POST", route, {"agent": agent.name, **data}).get("text", "")

    # -- mirroring the host's work into the local project

    def sync(self) -> None:
        with self._lock:
            st = self._req("GET", "/collab/state", self.cursor)
            self.apply(st)

    def _keep_gist(self, gist: str) -> None:
        if gist == self.gist or not (gist == "" or identity.GIST.match(gist)):
            return
        self.gist = gist
        proj = self.net.project_row(self.slug)
        cfg = json.loads(proj["config"])
        cfg.setdefault("link", {})["gist"] = gist
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    def apply(self, st: dict) -> None:
        if "gist" in st:
            self._keep_gist(str(st["gist"] or ""))
        if self.fork:
            return self._apply_fork(st)
        net, slug = self.net, self.slug
        proj = net.project_row(slug)
        pid = proj["id"]
        if "sms_profile" in st:
            net.sms.follow_host(slug, st["sms_profile"])
        mine = f"{self.member}."
        if st.get("symbols") is not None or st.get("functions") is not None:
            net.apply_remote_layout(slug, st.get("functions") or [], st.get("symbols") or [])
        if st.get("context") and _contained(st["context"]["header"]):
            net.apply_remote_context(slug, st["context"]["header"], st["context"]["version"])
            self.cursor["v"] = st["context"]["version"]
        # by ROM offset and name when the host sends them (banks share CPU addresses), by address from older hosts
        for m in st.get("matched") or []:
            if _contained(m["matched_source"]):
                net.apply_remote_match(slug, m["vram"], m["matched_source"], m["agent"] or "host", name=m.get("name"),
                                       rom_offset=m.get("rom_offset"), size=m.get("size"))
        for b in st.get("best") or []:
            if _contained(b["source"]):
                net.apply_remote_best(slug, b["vram"], b["score"], b["source"], name=b.get("name"),
                                      rom_offset=b.get("rom_offset"), size=b.get("size"))
        for ev in st.get("events") or []:  # (before the documented ones: one sent back may be documented again since)
            if ev["kind"] == "reopen" and str(ev["detail"]).endswith(": sent back to document again"):
                net.apply_remote_sent_back(slug, str(ev["detail"]).rsplit(":", 1)[0])
        if st.get("documented"):
            net.apply_remote_documented(slug, st["documented"])
        for n in st.get("notes") or []:
            net.apply_remote_note(slug, n["id"], n["vram"], n["text"], n["agent"] or "host", n["created_at"],
                                  name=n.get("function"), rom_offset=n.get("rom_offset"))
        for ev in st.get("events") or []:
            if ev["kind"] in ("match", "claim", "release", "context", "rename", "split", "merge", "collab", "documented") \
                    and not (ev["agent"] or "").startswith(mine):
                net.remote_event(pid, ev["kind"], ev["detail"], ev["agent"] or "host", ev["created_at"])
        self.claims = [c for c in st.get("claims") or [] if not (c["agent"] or "").startswith(mine)]
        self.members = st.get("members") or []
        self.cursor["e"], self.cursor["a"] = st.get("e", self.cursor["e"]), st.get("a", self.cursor["a"])

    def _apply_fork(self, st: dict) -> None:
        """A git project's room: follow the host's commits, then mirror matches, notes, events, claims."""
        net, slug = self.net, self.slug
        pid = net.project_row(slug)["id"]
        mine = f"{self.member}."
        if st.get("head") and st["head"] != self.head and (self.held or {}).get("head") != st["head"]:
            self.pull(st.get("branch") or "")
        for m in st.get("matched") or []:
            net.apply_remote_match_named(slug, m["name"], m["agent"] or "host")
        for n in st.get("notes") or []:
            net.apply_remote_note_named(slug, n["function"], n["text"], n["agent"] or "host", n["created_at"])
        for ev in st.get("events") or []:
            if ev["kind"] in ("match", "claim", "release", "context", "split", "collab", "link") \
                    and not (ev["agent"] or "").startswith(mine):
                net.remote_event(pid, ev["kind"], ev["detail"], ev["agent"] or "host", ev["created_at"])
        self.claims = [c for c in st.get("claims") or [] if not (c["agent"] or "").startswith(mine)]
        self.members = st.get("members") or []
        self.cursor["e"] = st.get("e", self.cursor["e"])

    def pull(self, ref: str) -> bool:
        """Bring the host's new commits into this tree (while no agent here is building in it), rebuild what
        changed and measure again. Returns whether anything came. Commits that change more than a room's work (the
        build setup: building runs it here) wait in `held` until the member follows them (follow_held)."""
        forks = self.net.forks
        proj, fp, _ = forks.get(self.slug)
        data = self._raw("/collab/bundle", {"have": self.head})
        if data:
            with forks._tree(proj).exclusive():
                tip = forkroom.fetch_bundle(fp.root, data, ref or forkroom.branch(fp.root))
                here = self.head or forkroom.head(fp.root)
                since = self.trusted if self.trusted and forkroom.is_ancestor(fp.root, self.trusted, tip) else here
                self.held = forkroom.hold(fp.root, since, tip) if since != tip else None
                if self.held:
                    return False
                forkroom.follow(fp.root, tip)
                forks._build_all(proj, fp)
        else:
            tip = self._req("GET", "/collab/state", {"e": self.cursor["e"]}).get("head") or self.head
        self.head = tip
        forkroom.save_head(self.net, self.slug, tip)
        if data:
            forks.sync(self.slug, log=lambda *_: None)
        return bool(data)

    def follow_held(self, head: str) -> bool:
        """The member chose to follow the host's commits up to `head` though they change the project's build setup."""
        with self._lock:
            if not self.held or self.held["head"] != head:
                raise NetworkError("nothing is waiting to be followed, or the host has moved on: look again")
            self.trusted, self.held = head, None
            proj = self.net.project_row(self.slug)
            cfg = json.loads(proj["config"])
            cfg.setdefault("link", {})["trusted"] = head
            with self.net.store.tx() as db:
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            return self.pull("")

    AWAY, LOOK_EVERY = 60, 120  # how long the host is away before asking where the room is, and how often

    def find_again(self, now: float | None = None) -> bool:
        """The host has been away a while: if the room said where it is now in its gist (a quick tunnel's address
        changes each time its app starts), check the room there shows the key this member joined with, and carry on
        there."""
        now = time.time() if now is None else now
        if not self.key or now - self.last_ok < self.AWAY or now - self._looked < self.LOOK_EVERY:
            return False
        self._looked = now
        url = identity.lookup(self.key, self.gist, self.github, now)
        if not url or identity.same_url(url, self.url):
            return False
        try:
            if identity.identify(self.client, url, self.room) != self.key:
                return False
            r = self.client.get(f"{url}/collab/whoami", params={"room": self.room},
                                headers={"authorization": f"Bearer {self.token}", "x-room": self.room})
        except (NetworkError, httpx.HTTPError):
            return False
        if r.status_code != 200:
            return False
        self.url = url.rstrip("/")
        proj = self.net.project_row(self.slug)
        cfg = json.loads(proj["config"])
        cfg.setdefault("link", {})["url"] = self.url
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        self.net.remote_event(proj["id"], "collab", f"the room moved to {self.url}: followed it", "host", now)
        return True

    def run(self, every: float = 2.0) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.sync()
                except NetworkError:
                    try:
                        self.find_again()
                    except Exception as e:  # keep syncing; show the problem in the GUI
                        self.error = f"{type(e).__name__}: {e}"
                except Exception as e:  # keep syncing; show the problem in the GUI
                    self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(every)
        threading.Thread(target=loop, daemon=True, name=f"link-{self.slug}").start()

    def stop(self) -> None:
        self._stop.set()


def _contained(text: str) -> bool:
    """Whether code from the room names no file outside its compile (compiler.check_includes): the host's checks may
    not be this computer's, so a member checks what it takes too."""
    try:
        check_includes(text or "")
        return True
    except SourceRejected:
        return False


def _hello(http: httpx.Client, link: str) -> tuple[str, str, dict]:
    """The room a link is for, as its host describes it, with its key (hello["key"]) once the host has shown it
    holds it: the link's key when the link has one (identity.identify)."""
    url, code = parse_invite(link)
    code = code.split("#", 1)[0]
    frag = _fragment(link)
    try:
        r = http.get(f"{url}/collab/hello", params={"invite": code, "room": frag.get("room", "")})
    except httpx.HTTPError:
        raise NetworkError("can't reach that room: is the host's decomp.wtf open and sharing?") from None
    hello = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise NetworkError(hello.get("error", "the invite didn't work"))
    hello["key"] = identity.identify(http, url, hello["room"])
    if frag.get("key") and hello["key"] != frag["key"]:
        raise NetworkError("the host at that address isn't the room this link was made for: its key is another")
    return url, code, hello


def _linked_project(net, sha1: str | None, room: str):
    for p in net.store.all("SELECT slug, config FROM projects"):
        cfg = json.loads(p["config"])
        if forkroom.project_identity(cfg) == sha1 and (cfg.get("link") or {}).get("room") == room:
            return p["slug"], cfg["link"]
    return None, None


def reconnect(net, slug: str, link: str, log=print, client: httpx.Client | None = None, start: bool = True) -> str:
    """Guest: the host restarted and shared a new link. Reconnect this project as the same member if the host still
    knows us, or join again with the invite."""
    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    proj = net.project_row(slug)
    cfg = json.loads(proj["config"])
    if forkroom.project_identity(cfg) != hello.get("sha1"):
        raise NetworkError(f"that link is for {hello['title']}, a different game than this project")
    info = dict(cfg.get("link") or {})
    if info.get("key") and hello["key"] != info["key"]:
        raise NetworkError("that link is for another room on this game, not the one this project is in")
    info["key"] = hello["key"] or info.get("key")
    if info.get("token"):
        r = http.get(f"{url}/collab/whoami", params={"room": hello["room"]},
                     headers={"authorization": f"Bearer {info['token']}", "x-room": hello["room"]})
        if r.status_code == 200:
            old = net.links.pop(slug, None)
            if old is not None:
                old.stop()
            net.link_project(slug, info | {"url": url, "room": hello["room"], "member": r.json()["name"]},
                             client=client, start=start)
            log(f"reconnected to {hello['title']} as {r.json()['name']}")
            return slug
    name = info.get("member") or "friend"
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"],
                                                                    **_answer(hello, own_copy(net, slug))})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    old = net.links.pop(slug, None)
    if old is not None:
        old.stop()
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"], "head": info.get("head", ""), "key": info["key"]},
                     client=client, start=start)
    log(f"joined {hello['title']} again as {joined['name']}")
    return slug


def join_room(net, link: str, name: str, rom: bytes | Path, log=print, client: httpx.Client | None = None,
              start: bool = True, trust: bool = False) -> str:
    """Guest: check the invite, check the ROM is the same game, join, import it locally, link. Returns the slug.
    If this game is already linked to that room here, reconnect instead (no ROM needed). `rom`: the game's bytes or
    the file (a GameCube or Wii disc is gigabytes: never read whole). `trust`: the member trusts the host with this
    computer, for a git project whose build setup comes from the host (forkroom.clone)."""
    from ..rom import detect

    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    slug, _info = _linked_project(net, hello["sha1"], hello["room"])
    if slug:
        return reconnect(net, slug, link, log=log, client=client, start=start)
    if not rom:
        raise NetworkError("pick your own copy of the game to join")
    if hello.get("kind") == "fork":
        return _join_fork(net, http, url, code, hello, name, rom, log, client, start, trust)
    named = rom.name if isinstance(rom, Path) else ""  # a Master System ROM with no header is told by its name
    if isinstance(rom, Path):
        rom = rom.read_bytes()
    picked = detect.identify(rom, named)
    if picked.info["sha1"] != hello["sha1"]:
        raise NetworkError(f"this ROM isn't the same as the host's: the room is for {hello['title']} "
                           f"({hello['game_code']}, {hello['region']}), and every member needs their own copy of exactly that version")
    log(f"joining {hello['title']} ({hello['functions']} functions, {hello['matched']} matched)")
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"],
                                                                    **_answer(hello, forkroom.in_memory(picked.data))})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    slug = net.import_rom(rom, toolchain=hello["compiler"]["toolchain"], flags=hello["compiler"]["flags"],
                          confirmed=True, log=log)
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"], "key": hello["key"]}, client=client, start=start)
    log(f"linked as {joined['name']}: your agents now work with the room")
    return slug


def _wait_to_be_let_in(http: httpx.Client, url: str, room: str, headers: dict, log, every: float = 2.0,
                       patience: float = 1800.0) -> None:
    """A room that approves its members lets them in by hand: the host's tree is only sent once they have."""
    log("waiting for the host to let you in")
    deadline = time.time() + patience
    while True:
        try:
            r = http.get(f"{url}/collab/whoami", params={"room": room}, headers=headers)
        except httpx.HTTPError:
            raise NetworkError("can't reach that room: is the host's decomp.wtf open and sharing?") from None
        if r.status_code == 200:
            log("the host let you in")
            return
        err = r.json().get("error", "") if r.headers.get("content-type", "").startswith("application/json") else ""
        if "waiting for the host" not in err:
            raise NetworkError("the host didn't let you in" if "invalid API token" in err else err or "couldn't join")
        if time.time() > deadline:
            raise NetworkError("the host hasn't let you in yet: ask them to approve you, then join again")
        time.sleep(every)


def _join_fork(net, http: httpx.Client, url: str, code: str, hello: dict, name: str, game: bytes | Path, log,
               client: httpx.Client | None, start: bool, trust: bool = False) -> str:
    """Join a git project's room: the host's tree (forkroom.clone), the member's own copy of the game placed where
    the project wants it and built with the member's own tools, then linked."""
    import tempfile

    from ..forks.catalog import PLATFORMS

    if not trust and not forkroom.verifiable(hello):
        raise NetworkError(forkroom.UNVERIFIABLE)  # before anything is fetched, or the host hears of this member
    tmp = None
    if not isinstance(game, Path):
        tmp = tempfile.NamedTemporaryFile(prefix="decomp-join-", suffix=".game", delete=False)
        tmp.write(game)
        tmp.close()
        game = Path(tmp.name)
    try:
        if hello.get("sha1") and forkroom.game_identity(game) != hello["sha1"]:
            raise NetworkError(f"this isn't the same game as the host's: the room is for {hello['title']} "
                               f"({PLATFORMS.get(hello.get('platform'), hello.get('platform') or '')}, "
                               f"{hello.get('version') or 'the same version'}), and every member needs their own copy of "
                               "exactly that version")
        log(f"joining {hello['title']} ({hello['functions']} functions, {hello['matched']} matched)")
        r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello.get("sha1") or "",
                                                                        **_answer(hello, forkroom.game_bytes(game))})
        joined = r.json()
        if r.status_code != 200:
            raise NetworkError(joined.get("error", "couldn't join"))
        headers = {"authorization": f"Bearer {joined['token']}", "x-room": hello["room"]}
        if joined.get("waiting"):
            _wait_to_be_let_in(http, url, hello["room"], headers, log)

        def get_bundle(have: str) -> bytes:
            res = http.get(f"{url}/collab/bundle", params={"have": have, "room": hello["room"]}, headers=headers,
                           timeout=httpx.Timeout(600, connect=15))
            if res.status_code == 204:
                return b""
            if res.status_code != 200:
                raise NetworkError((res.json() if res.headers.get("content-type", "").startswith("application/json")
                                    else {}).get("error") or "the host couldn't send the project")
            return res.content

        slug = net.forks._free_slug(re.sub(r"[^a-z0-9]+", "-", hello["title"].lower()).strip("-")[:40] or "room")
        dest = net.workspace / slug
        try:
            forkroom.clone(dest, hello, get_bundle, log=log, trust=trust)
            forkroom.register_member(net, slug, dest, hello, hello["title"])
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        info = {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                "host_title": hello["title"], "head": forkroom.head(dest), "key": hello["key"]}
        net.link_project(slug, info, client=client, start=False)  # its agents wait for the room from the start
        log("building the project with your copy of the game and your tools")
        net.forks.setup(slug, game, game.name if tmp is None else "", log=log, move=tmp is not None)
        if start:
            net.links[slug].run()
        log(f"linked as {joined['name']}: your agents now work with the room")
        return slug
    finally:
        if tmp is not None:
            Path(tmp.name).unlink(missing_ok=True)


def cloudflared_available() -> bool:
    return toolchains.get("cloudflared").supported or bool(shutil.which("cloudflared"))


_addresses: tuple[float, list[dict]] = (0.0, [])


def local_addresses() -> list[dict]:
    """Addresses teammates might reach this computer at: the LAN address, and Tailscale's when it's installed."""
    global _addresses
    if time.time() - _addresses[0] < 600:
        return _addresses[1]
    _addresses = (time.time(), _local_addresses())
    return _addresses[1]


def _local_addresses() -> list[dict]:
    import socket

    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the outgoing interface
            ip = sock.getsockname()[0]
        if not ip.startswith("127."):
            out.append({"label": "this network (LAN)", "address": f"http://{ip}:6481"})
    except OSError:
        pass
    ts = shutil.which("tailscale")
    if ts:
        try:
            res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5,
                                 encoding="utf-8", errors="replace")
            me = json.loads(res.stdout).get("Self") or {}
            name = (me.get("DNSName") or "").rstrip(".")
            ips = [a for a in me.get("TailscaleIPs") or [] if "." in a]
            if name or ips:
                out.append({"label": "Tailscale (your tailnet)", "address": f"http://{name or ips[0]}:6481"})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return out
