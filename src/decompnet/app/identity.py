"""A room's identity, apart from its address.

Every room has a key of its own (Ed25519), made the first time it's shared and kept with the project. Its public half
is in every invite link (#room=<slug>&key=<key>), and the host proves it holds the key whenever a member asks
(/collab/ping): so a member can tell the room is the one they joined, wherever it is now.

A quick tunnel's address changes each time the host's app starts. A room shared through one can say where it is now:
a note of its address, signed with its key, in a secret gist on the host's GitHub account (the host connects GitHub
with a token that can only make gists). The room tells its members which gist; a member whose host went away reads
it (no GitHub account needed), checks the signature with the key they joined with, and carries on at the new address.
Nobody runs a server for it, and GitHub (or anyone) can't send a member anywhere the room didn't sign.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GITHUB = "https://api.github.com"
NOTE = "decomp-wtf-room.json"  # the gist's one file
POINTER_LIFE = 86400  # how long a note of the room's address is good for...
REFRESH_LEFT = 12 * 3600  # ...written again once less than this is left (two writes a day per room)
KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")  # a public key, base64url without padding
GIST = re.compile(r"^[0-9a-f]{20,40}$")
QUICK_TUNNEL = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")  # the only addresses a note may name

_http = httpx.Client(timeout=httpx.Timeout(15, connect=10))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_secret() -> str:
    """A room's new private key (kept in the project's settings on the host's computer, never sent)."""
    return _b64(Ed25519PrivateKey.generate().private_bytes_raw())


def public_key(secret: str) -> str:
    return _b64(Ed25519PrivateKey.from_private_bytes(_unb64(secret)).public_key().public_bytes_raw())


def sign(secret: str, message: str) -> str:
    return _b64(Ed25519PrivateKey.from_private_bytes(_unb64(secret)).sign(message.encode()))


def verify(key: str, message: str, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(key)).verify(_unb64(signature), message.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def ping_message(url: str, room: str, nonce: str) -> str:
    """What a host signs to show it's the room `room` at `url`, for a member's fresh `nonce`."""
    return f"decomp.wtf room ping v1\n{url}\n{room}\n{nonce}"


def pointer_message(key: str, url: str, t: int, expires: int) -> str:
    """What a room signs to say it's at `url` (as of `t`, until `expires`)."""
    return f"decomp.wtf room pointer v1\n{key}\n{url}\n{t}\n{expires}"


def same_url(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def identify(http: httpx.Client, url: str, room: str) -> str | None:
    """The key of the room `room` at `url`, once its host has signed a fresh nonce with it (None: a host from before
    room keys). Raises NetworkError when the host answers but can't show it."""
    from ..network import NetworkError

    nonce = _b64(os.urandom(18))
    try:
        r = http.get(f"{url.rstrip('/')}/collab/ping", params={"room": room, "nonce": nonce})
    except httpx.HTTPError:
        raise NetworkError("can't reach that room: is the host's decomp.wtf open and sharing?") from None
    if r.status_code == 404:
        return None
    got = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    key = str(got.get("key") or "")
    if r.status_code != 200 or not KEY.match(key) or not verify(key, ping_message(url.rstrip("/"), room, nonce),
                                                                  str(got.get("signature") or "")):
        raise NetworkError(got.get("error") or "the host at that address couldn't show it's the room it says it is")
    return key


def _github(token: str | None = None) -> dict:
    return {"accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28",
            **({"authorization": f"Bearer {token}"} if token else {})}


def _error(r: httpx.Response) -> str:
    got = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return str(got.get("message") or got.get("error") or f"GitHub answered {r.status_code}")


def github_login(token: str, http: httpx.Client | None = None) -> str:
    """The GitHub account a token belongs to, once it's shown it can make gists (a classic token with the gist scope:
    fine-grained ones can't). Raises ValueError."""
    try:
        r = (http or _http).get(f"{GITHUB}/user", headers=_github(token))
    except httpx.HTTPError as e:
        raise ValueError(f"couldn't reach GitHub ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ValueError(f"GitHub didn't take that token: {_error(r)}")
    scopes = {s.strip() for s in r.headers.get("x-oauth-scopes", "").split(",")}
    if "gist" not in scopes:
        raise ValueError("that token can't make gists: make a classic token with only the gist scope")
    return str(r.json().get("login") or "")


def publish(secret: str, url: str, token: str, gist: str = "", http: httpx.Client | None = None,
            now: float | None = None) -> tuple[str, dict]:
    """Say where the room is now, in the host's gist (a new secret one the first time, or when it's gone): returns
    (the gist's id, the note {"url", "t", "expires", "sig"}). Raises httpx.HTTPError, or ValueError when GitHub
    turns it down."""
    http = http or _http
    t = int(now if now is not None else time.time())
    expires = t + POINTER_LIFE
    key = public_key(secret)
    note = {"key": key, "url": url, "t": t, "expires": expires, "sig": sign(secret, pointer_message(key, url, t, expires))}
    files = {NOTE: {"content": json.dumps(note, indent=1)}}
    r = http.patch(f"{GITHUB}/gists/{gist}", json={"files": files}, headers=_github(token)) if gist else None
    if r is None or r.status_code == 404:  # never made, or deleted on GitHub since
        r = http.post(f"{GITHUB}/gists", headers=_github(token), json={
            "description": "Where a decomp.wtf room is now (a signed note its members' apps read)", "public": False,
            "files": files})
    if r.status_code not in (200, 201):
        raise ValueError(_error(r))
    return str(r.json()["id"]), note


def lookup(key: str, gist: str, http: httpx.Client | None = None, now: float | None = None) -> str | None:
    """Where the room with this key says it is now, from its gist (None: it hasn't said, the note expired, or it
    isn't signed with the key: what's in the gist is never taken at its word)."""
    if not KEY.match(key or "") or not GIST.match(gist or ""):
        return None
    try:
        r = (http or _http).get(f"{GITHUB}/gists/{gist}", headers=_github())
        note = json.loads(r.json()["files"][NOTE]["content"]) if r.status_code == 200 else {}
        url, t, expires, sig = str(note["url"]), int(note["t"]), int(note["expires"]), str(note["sig"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    if expires < (now if now is not None else time.time()) or not QUICK_TUNNEL.match(url):
        return None
    return url if verify(key, pointer_message(key, url, t, expires), sig) else None
