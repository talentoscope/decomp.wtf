"""The assembly track in a room: a member's documented routine is judged again by the room's host, which keeps it;
members mirror the routines the room documents, and the ones its host sends back from review."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from decompnet.app import collab
from decompnet.network import NetworkError
from decompnet.rom import gb_asm
from tests.test_documented import make_game
from tests.test_gb_asm import GOOD

pytestmark = pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")
TEXT = "DEF wMenuOpen EQU $c0a2\n" + GOOD


@pytest.fixture
def room(tmp_path):
    (tmp_path / "host").mkdir()
    (tmp_path / "member").mkdir()
    host = make_game(tmp_path / "host")
    host.rooms["g"] = collab.Room(host, "g")
    client = TestClient(collab.host_app(host, host.rooms), base_url="https://abc.trycloudflare.com")
    token, name = host.rooms["g"].join("alex", "")
    member = make_game(tmp_path / "member")
    member.link_project("g", {"url": "https://abc.trycloudflare.com", "room": "g", "token": token, "member": name,
                              "host_title": "G", "key": ""}, client=client, start=False)
    agent, _ = member.local_agent("claude-1")
    return host, member, agent


def _fn(net, rom_offset=0x8A10):
    return net.store.one("SELECT * FROM functions WHERE rom_offset=?", rom_offset)


def test_a_member_claims_a_routine_from_the_host(room):
    host, member, agent = room
    brief = member.claim(agent, "g", track="asm")
    assert "CLAIMED (assembly track): g/Call_002_4a10" in brief
    claim = host.store.one("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id")
    assert claim["name"] == "alex.claude-1"


def test_the_host_judges_a_members_routine_again_and_keeps_it(room):
    host, member, agent = room
    member.claim(agent, "g", track="asm")
    out = member.compile_and_diff(agent, "Call_002_4a10", TEXT, "g")
    assert out.startswith("✅ DOCUMENTED: ClearMenuBackground") and "Shared with the room" in out, out
    for net in (host, member):  # the host keeps it, and the member has the host's name for it and what it named
        fn = _fn(net)
        assert (fn["status"], fn["name"]) == ("documented", "ClearMenuBackground"), net
        assert net.store.one("SELECT address FROM symbols WHERE name='wMenuOpen'")["address"] == 0xC0A2
    assert not _fn(member)["meta"] or "sample" not in _fn(member)["meta"]  # (the host picks what a person looks over)


def test_what_fails_on_the_members_copy_doesnt_reach_the_host(room):
    host, member, agent = room
    out = member.compile_and_diff(agent, "Call_002_4a10", TEXT.replace("TILEMAP0", "$9800"), "g")
    assert out.startswith("❌ not yet")
    assert host.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0


def test_a_routine_the_host_wont_take_stays_on_the_track_until_the_room_says(room):
    host, member, agent = room
    writer = host.authenticate(host.register_agent("writer"))
    host.document_function(writer, "g", "Call_002_4a10", TEXT.replace("ClearMenuBackground", "ClearMenuMap"))
    out = member.compile_and_diff(agent, "Call_002_4a10", TEXT, "g")
    assert out.startswith("❌ it passes every check on your copy, but the room's host didn't take it") and "documented already" in out
    assert _fn(member)["status"] == "asm"
    member.links["g"].sync()  # the room's documented routine, mirrored with its text
    fn = _fn(member)
    assert (fn["status"], fn["name"]) == ("documented", "ClearMenuMap") and "ClearMenuMap:" in fn["matched_source"]


def test_a_routine_the_host_sends_back_goes_back_on_the_members_track(room):
    host, member, agent = room
    member.compile_and_diff(agent, "Call_002_4a10", TEXT, "g")
    with pytest.raises(NetworkError, match="host looks"):
        member.review_documented("g", "ClearMenuBackground", False, "a member can't send it back")
    host.review_documented("g", "ClearMenuBackground", False, "it runs every frame, not only in menus")
    member.links["g"].sync()
    fn = _fn(member)
    assert fn["status"] == "asm" and "ClearMenuBackground:" in fn["best_source"] and fn["matched_source"] is None
    assert member.compile_and_diff(agent, "ClearMenuBackground", TEXT.replace("ClearMenuBackground", "ClearBackground"),
                                   "g").startswith("✅ DOCUMENTED: ClearBackground")
    assert _fn(host)["status"] == "documented"


def test_a_member_keeps_only_text_the_judge_would_take(room):
    host, member, agent = room
    row = {"name": "Call_002_4a10", "rom_offset": 0x8A10, "size": 17, "vram": 0x4A10, "agent": "host"}
    member.apply_remote_documented("g", [row | {"source": 'INCBIN "/etc/passwd"\n' + GOOD}])
    assert _fn(member)["status"] == "asm"
    member.apply_remote_documented("g", [row | {"source": GOOD}])
    assert _fn(member)["status"] == "documented"
