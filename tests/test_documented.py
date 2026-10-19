"""Documenting a hand-written Game Boy routine through the app (engine.document_function): judged by rom/gb_asm.py,
recorded as an attempt, and once accepted kept as the function's text, its DEF names the project's, its label its
name."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.rom import gb_asm
from tests.test_gb_asm import CODE, GOOD

pytestmark = pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")
HAND = json.dumps({"asm_code": "reads hl before setting it: SDCC passes arguments in a, e, de and bc, never in hl"})


def make_game(tmp_path) -> RomNetwork:
    """A Game Boy Color project "g" with one hand-written routine to document (the judge's test routine, bank 2)."""
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "gbc", "arch": "sm83", "rom": {"size": 0x10000, "mbc": "MBC5", "file": "rom.gbc"},
           "compiler": {"toolchain": "gbdk-2020", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("g", "G", "", "sm83", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", 0, ""))
        rows = [("Call_002_4a10", 0x8A10, 0x4A10, CODE, "asm", HAND), ("FillTilemapZero", 0x0F31, 0x0F31, b"\xc9", "open", None),
                ("Call_002_5000", 0x9000, 0x5000, b"\xff" * 4, "asm", json.dumps({"asm_code": "padding (00 or FF bytes only), not a function"}))]
        for name, flat, cpu, code, status, meta in rows:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status,meta) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, name, len(code), 1, code, "[]", "", "bank002", cpu, flat, status, meta))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, cpu, len(code), "func"))
    return net


@pytest.fixture
def game(tmp_path):
    net = make_game(tmp_path)
    return net, net.authenticate(net.register_agent("writer"))


def test_an_accepted_routine_is_documented_under_its_own_name(game):
    net, agent = game
    text = "DEF wMenuOpen EQU $c0a2\n" + GOOD
    out = net.document_function(agent, "g", "Call_002_4a10", text)
    assert out.startswith("✅ DOCUMENTED: ClearMenuBackground"), out
    fn = net.store.one("SELECT * FROM functions WHERE rom_offset=?", 0x8A10)
    assert fn["status"] == "documented" and fn["name"] == "ClearMenuBackground" and "DEF wMenuOpen" not in fn["matched_source"]
    assert net.store.one("SELECT address FROM symbols WHERE name='wMenuOpen'")["address"] == 0xC0A2
    assert net.store.one("SELECT via, score FROM attempts")[:] == ("asm", 0)
    with pytest.raises(NetworkError, match="documented already"):
        net.document_function(agent, "g", "ClearMenuBackground", text)


def test_a_routine_that_isnt_there_yet_says_why(game):
    net, agent = game
    out = net.document_function(agent, "g", "Call_002_4a10", "DEF wMenuOpen EQU $c0a2\n" + GOOD.replace("TILEMAP0", "$9800"))
    assert out.startswith("❌ not yet (17 bytes, 17 the same)") and "5. line 9" in out
    fn = net.store.one("SELECT * FROM functions WHERE rom_offset=?", 0x8A10)
    assert fn["status"] == "asm" and fn["best_score"] == 1 and fn["attempts"] == 1


def test_only_hand_written_routines_set_aside_from_c_are_documented(game):
    net, agent = game
    with pytest.raises(NetworkError, match="isn't a hand-written routine"):
        net.document_function(agent, "g", "FillTilemapZero", GOOD)  # open: the C track's
    with pytest.raises(NetworkError, match="isn't a hand-written routine"):
        net.document_function(agent, "g", "Call_002_5000", GOOD)  # padding: not code


def test_someone_elses_claim_is_respected(game):
    net, agent = game
    other = net.authenticate(net.register_agent("other"))
    fid = net.store.one("SELECT id FROM functions WHERE name='Call_002_4a10'")["id"]
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, other.id, 0, 1e12))
    with pytest.raises(NetworkError, match="claimed by other"):
        net.document_function(agent, "g", "Call_002_4a10", GOOD)


def test_the_map_and_the_overview_count_documented_routines_apart(game, monkeypatch):
    net, agent = game
    monkeypatch.setattr(RomNetwork, "REVIEW_SAMPLE", 1.0)  # (every one picked for a person to look over)
    before = {r["name"]: r for r in net.function_map("g")}
    assert before["Call_002_4a10"].get("track") and not before["Call_002_5000"].get("track")  # padding isn't code
    net.document_function(agent, "g", "Call_002_4a10", "DEF wMenuOpen EQU $c0a2\n" + GOOD)
    row = {r["name"]: r for r in net.function_map("g")}["ClearMenuBackground"]
    assert row["status"] == "documented" and row["sample"]
    ov = net.overview("g")
    assert (ov["documented"], ov["documented_bytes"]) == (1, len(CODE))
    assert ov["mapped"]["code"] == len(CODE) + 1 and ov["mapped"]["to_document"] == 0  # (and FillTilemapZero's one byte)
    detail = net.function_detail("g", "ClearMenuBackground")
    assert detail["source_kind"] == "documented" and detail["sample"] and detail["track"]


def test_a_person_looks_over_a_documented_routine(game, monkeypatch):
    net, agent = game
    monkeypatch.setattr(RomNetwork, "REVIEW_SAMPLE", 1.0)
    net.document_function(agent, "g", "Call_002_4a10", "DEF wMenuOpen EQU $c0a2\n" + GOOD)
    with pytest.raises(NetworkError, match="what's wrong"):
        net.review_documented("g", "ClearMenuBackground", False, "no")
    net.review_documented("g", "ClearMenuBackground", True)
    assert not net.function_detail("g", "ClearMenuBackground")["sample"]
    net.review_documented("g", "ClearMenuBackground", False, "the name says menu, but it runs on every frame")
    fn = net.store.one("SELECT * FROM functions WHERE name='ClearMenuBackground'")
    assert fn["status"] == "asm" and "ClearMenuBackground:" in fn["best_source"] and fn["matched_source"] is None
    assert "sent back from review" in net.store.one("SELECT text FROM notes WHERE function_id=? ORDER BY id DESC", fn["id"])["text"]
    assert "Best attempt so far" in net.get_function("ClearMenuBackground", "g")
