"""The public room list: rooms their hosts chose to list, kept in a GitHub repository (ROOMS_REPO) anyone can read.

A host lists a public room by opening an issue there with a listing this app writes and signs with the room's key
(listing()). That repository's Action checks the signature, that the room answers with that key, that it's public,
and that its game and product code are what the listing says; then it adds the room to rooms.json. It looks at every
listed room again every few hours (members, progress) and takes rooms off that stay away for a week. Unlisting is a
signed listing too, so only the room's host can list or unlist it.

An entry holds what it takes to find the room: its key, and the host's gist for a room shared through a quick tunnel
(its address changes; the gist says where it is now, signed: identity.py) or the room's own address. Then its
platform, product code and game identity (a sha1, which matches the games on this computer), never a game's title:
this app shows the title of a game you have. Nothing from the list is taken at its word: an address comes from the
room's signed note, and joining checks the room's key (identity.identify).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse

import httpx

from ..network import NetworkError
from . import forkroom, identity

ROOMS_REPO = os.environ.get("DECOMP_ROOMS_REPO", "SynapticDreams/decomp.wtf-rooms")
LIST_URL = f"https://raw.githubusercontent.com/{ROOMS_REPO}/main/rooms.json"
NEW_ISSUE = f"https://github.com/{ROOMS_REPO}/issues/new"
PREFIX = "decomp.wtf-listing:"  # how a listing starts, so it's easy to tell in an issue
MAX_NOTE = 200

_http = httpx.Client(timeout=httpx.Timeout(20, connect=10), follow_redirects=True)

# what an entry of rooms.json may hold, and how it must look (the list is read from a repository, so checked here)
_TEXT = {
    "key": identity.KEY, "room": re.compile(r"^[\w-]{1,64}$"), "gist": re.compile(r"^([0-9a-f]{20,40})?$"),
    "url": re.compile(r"^(https?://[^\s/?#]{1,200})?$"), "platform": re.compile(r"^[a-z0-9]{1,12}$"),
    "code": re.compile(r"^[\w.-]{0,32}$"), "sha1": re.compile(r"^([0-9a-f]{40})?$"),
    "kind": re.compile(r"^(rom|fork|scratch)$"), "repo": re.compile(r"^(https://[^\s]{1,200})?$"),
}
_NUMBERS = ("listed", "seen", "functions", "matched", "members", "limit")


def listing_message(fields: dict) -> str:
    """What a room signs to be listed or unlisted: the listing's fields, in one way of writing them (the list's
    Action writes them the same way)."""
    return "decomp.wtf room listing v1\n" + json.dumps(fields, sort_keys=True, separators=(",", ":"))


def listing(net, room, action: str = "list", note: str = "", now: float | None = None) -> str:
    """A signed listing (action "list") or unlisting ("unlist") of a room this computer hosts, as the text for the
    list's issue form. Refuses a room nobody could find from the list: one that isn't public, or one shared through
    a quick tunnel whose host hasn't connected GitHub (its address changes; the gist says where it is)."""
    if action not in ("list", "unlist"):
        raise NetworkError("a room is listed or unlisted")
    cfg = room.cfg
    collab = cfg.get("collab") or {}
    tunnel = collab.get("mode", "tunnel") == "tunnel"
    fields = {"action": action, "key": room.key, "room": room.slug, "t": int(now if now is not None else time.time())}
    if action == "list":
        if room.settings["visibility"] != "public":
            raise NetworkError("only a public room can be on the list: make it public first (anyone with its address "
                               "and their own copy can then join)")
        if tunnel and not (room.settings["findable"] and room.gist):
            raise NetworkError("a room shared through a quick tunnel needs GitHub connected (Team tab), so the list can "
                               "find it when its address changes")
        note = " ".join(str(note or "").split())[:MAX_NOTE]
        kind = "rom" if not room.fork else "scratch" if cfg.get("origin") == "scratch" else "fork"
        fields |= {"gist": room.gist if tunnel else "", "url": "" if tunnel else room.url,
                   "platform": cfg.get("platform") or "", "sha1": forkroom.project_identity(cfg) or "",
                   "code": (cfg.get("rom") or {}).get("game_code") or cfg.get("version") or "",
                   "kind": kind, "repo": (cfg.get("repo_url") or "") if kind == "fork" else "", "note": note}
    signed = {**fields, "sig": identity.sign(room.secret, listing_message(fields))}
    return PREFIX + base64.urlsafe_b64encode(json.dumps(signed, separators=(",", ":")).encode()).decode().rstrip("=")


def issue_url(blob: str, action: str = "list") -> str:
    """The list's issue form, filled in with a listing: the host only has to submit it (signed in to GitHub)."""
    template = "list-a-room.yml" if action == "list" else "unlist-a-room.yml"
    return f"{NEW_ISSUE}?" + urllib.parse.urlencode({"template": template, "listing": blob,
                                                     "title": "List a room" if action == "list" else "Unlist a room"})


def clean(entry: dict) -> dict | None:
    """An entry of rooms.json as this app shows it, or None when it doesn't look like one."""
    if not isinstance(entry, dict):
        return None
    out = {}
    for name, pattern in _TEXT.items():
        value = entry.get(name, "")
        if not isinstance(value, str) or not pattern.match(value):
            return None
        out[name] = value
    if not (out["gist"] or out["url"]):
        return None
    for name in _NUMBERS:
        value = entry.get(name, 0)
        out[name] = value if isinstance(value, int) and value >= 0 else 0
    out["note"] = " ".join(str(entry.get("note") or "").split())[:MAX_NOTE]
    out["approve"] = entry.get("approve") is True
    return out


def fetch(http: httpx.Client | None = None) -> list[dict]:
    """The listed rooms, from the list's repository (raises NetworkError when it can't be read)."""
    try:
        r = (http or _http).get(LIST_URL)
        data = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        raise NetworkError("couldn't read the room list from GitHub right now")
    return [e for e in (clean(x) for x in data["rooms"]) if e is not None]


def link(entry: dict, github: httpx.Client | None = None) -> str:
    """A listed room's link, where it is now: from the host's gist (signed by the room) or its own address. Joining
    with it checks the room's key."""
    url = identity.lookup(entry["key"], entry["gist"], github) if entry.get("gist") else entry.get("url")
    if not url:
        raise NetworkError("that room hasn't said where it is lately: its host may be away")
    return f"{url.rstrip('/')}/join/open#room={entry['room']}&key={entry['key']}"
