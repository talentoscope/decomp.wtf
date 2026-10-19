"""The public room list (roomlist.py): the signed listings a host submits to the list's repository, the list as the
app reads it from there, and the Open rooms page's API."""

import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from decompnet.app import collab, identity, roomlist
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError

from .test_room_identity import TOKEN, _project, github


def _decode(blob: str) -> dict:
    raw = blob.removeprefix(roomlist.PREFIX)
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


@pytest.fixture
def room(tmp_path):
    net = RomNetwork(tmp_path / "h.db", tmp_path / "h")
    slug = _project(net)
    cfg = net.cfg(net.project_row(slug)) | {"platform": "gc", "version": "GALE01", "program_sha1": "c" * 40,
                                            "origin": "scratch"}
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), slug))
    r = collab.Room(net, slug)
    r.open_invite(mode="tunnel")
    r.url = "https://abc-def.trycloudflare.com"
    net.rooms[slug] = r
    return net, slug, r


def test_only_a_public_room_the_list_can_find_is_listed(room):
    net, slug, r = room
    with pytest.raises(NetworkError, match="public"):
        roomlist.listing(net, r)
    r.configure(visibility="public")
    with pytest.raises(NetworkError, match="GitHub"):  # on a quick tunnel, with no gist to say where it is
        roomlist.listing(net, r)
    r._keep_gist("0123456789abcdef0123")
    signed = _decode(roomlist.listing(net, r, note="  menus\n first  "))
    sig = signed.pop("sig")
    assert identity.verify(r.key, roomlist.listing_message(signed), sig)
    assert signed | {"t": 0} == {"action": "list", "key": r.key, "room": slug, "t": 0, "gist": "0123456789abcdef0123",
                                 "url": "", "platform": "gc", "sha1": "c" * 40, "code": "GALE01", "kind": "scratch",
                                 "repo": "", "note": "menus first"}


def test_a_room_at_its_own_address_is_listed_by_it(room):
    net, slug, r = room
    r.configure(visibility="public")
    r.open_invite(keep=True, mode="address", address="https://decomp.example.org", port=6481)
    r.url = "https://decomp.example.org"
    signed = _decode(roomlist.listing(net, r))
    assert signed["url"] == "https://decomp.example.org" and signed["gist"] == ""


def test_unlisting_is_signed_too_and_needs_nothing_else(room):
    net, slug, r = room
    signed = _decode(roomlist.listing(net, r, "unlist"))  # (a private room may still come off the list)
    sig = signed.pop("sig")
    assert set(signed) == {"action", "key", "room", "t"} and signed["action"] == "unlist"
    assert identity.verify(r.key, roomlist.listing_message(signed), sig)


def test_the_issue_form_comes_filled_in(room):
    net, slug, r = room
    url = roomlist.issue_url("decomp.wtf-listing:abc", "list")
    assert url.startswith(f"https://github.com/{roomlist.ROOMS_REPO}/issues/new?")
    assert "template=list-a-room.yml" in url and "listing=decomp.wtf-listing%3Aabc" in url
    assert "template=unlist-a-room.yml" in roomlist.issue_url("x", "unlist")


def _entry(**change) -> dict:
    return {"key": "A" * 43, "room": "demo", "gist": "0123456789abcdef0123", "url": "", "platform": "n64",
            "sha1": "a" * 40, "code": "NDQE", "kind": "rom", "repo": "", "note": "hi", "listed": 1, "seen": 2,
            "functions": 95, "matched": 3, "members": 1, "limit": 0, "approve": True} | change


def test_an_entry_is_shown_only_when_it_looks_like_one():
    assert roomlist.clean(_entry()) == _entry()
    for bad in ({"key": "short"}, {"gist": "", "url": ""}, {"url": "javascript:alert(1)", "gist": ""},
                {"platform": "<b>"}, {"kind": "anything"}, {"repo": "http://plain.example"}, {"room": "a b"}):
        assert roomlist.clean(_entry(**bad)) is None, bad
    shown = roomlist.clean(_entry(matched=-4, members="many", note="x" * 500, approve="yes", title="a game"))
    assert shown["matched"] == 0 and shown["members"] == 0 and len(shown["note"]) == roomlist.MAX_NOTE
    assert shown["approve"] is False and "title" not in shown


def _repository(rooms) -> TestClient:
    async def raw(request):
        return JSONResponse({"version": 1, "rooms": rooms})
    path = roomlist.LIST_URL.split("raw.githubusercontent.com", 1)[1]
    return TestClient(Starlette(routes=[Route(path, raw)]), base_url="https://raw.githubusercontent.com")


def test_the_list_is_read_from_its_repository():
    assert roomlist.fetch(_repository([_entry(), {"key": "nope"}, "junk"])) == [_entry()]

    async def gone(request):
        return JSONResponse({"message": "Not Found"}, status_code=404)
    with pytest.raises(NetworkError, match="couldn't read"):
        roomlist.fetch(TestClient(Starlette(routes=[Route("/{p:path}", gone)]), base_url="https://raw.githubusercontent.com"))


def test_a_listed_rooms_link_comes_from_what_its_gist_says(room):
    net, slug, r = room
    gh, gists = github()
    gid, _ = identity.publish(r.secret, r.url, TOKEN, "", gh)
    entry = _entry(key=r.key, room=slug, gist=gid)
    assert roomlist.link(entry, gh) == f"{r.url}/join/open#room={slug}&key={r.key}"
    with pytest.raises(NetworkError, match="hasn't said"):
        roomlist.link(_entry(key=r.key, gist="f" * 20), gh)
    assert roomlist.link(_entry(gist="", url="https://decomp.example.org/")).startswith("https://decomp.example.org/join/open#")


def test_the_open_rooms_page_marks_the_games_this_computer_has(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    slug = _project(net, "mine")
    cfg = net.cfg(net.project_row(slug)) | {"program_sha1": "a" * 40}
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), slug))
    listed = [_entry(), _entry(key="B" * 43, sha1="b" * 40)]
    monkeypatch.setattr(roomlist, "fetch", lambda http=None: listed)
    mcp, manager, security = build(net, home / ".decomp", 6498, "s3cret")
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6498")
    client.cookies.set("decomp_live", "s3cret")
    got = {r["key"]: r for r in client.get("/api/rooms").json()["rooms"]}
    assert got["A" * 43]["have"] == slug and got["A" * 43]["title"] == "Game"  # the title from here, not the list
    assert got["B" * 43]["have"] is None and got["B" * 43]["title"] is None
    r = client.post("/api/rooms/link", json={"key": "C" * 43}, headers={"X-Decomp": "1"})
    assert r.status_code == 400 and "isn't on the list" in r.json()["error"]
    r = client.post("/api/collab/listing", json={"slug": slug}, headers={"X-Decomp": "1"})
    assert r.status_code == 400 and "share the room first" in r.json()["error"]
    manager.stop()
