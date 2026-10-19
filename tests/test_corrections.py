"""Corrections to a Game Boy game's split: agents say what the split got wrong about its code and data (with how they
know, a few an hour), a person sees and can undo each, and splitting the game again uses them; a symbol file the user
chooses names the project's functions, kept to its well-formed lines."""

from __future__ import annotations

import json

import pytest

from decompnet.app import agents, tools
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.rom import gb_mgbdis
from tests.test_gb_map import rom_with

needs_tools = pytest.mark.skipif(not gb_mgbdis.available(), reason="needs mgbdis and RGBDS")
WHY = "the code at 0150 reads these bytes with ld a,(hl+) in a loop"


@pytest.fixture
def game(tmp_path):
    """A Game Boy project made from its rows (no split needed): functions in banks 0 and 2."""
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    root = tmp_path / "game"
    root.mkdir()
    (root / "rom.gbc").write_bytes(rom_with({0x150: "af c9"}))
    cfg = {"kind": "rom", "platform": "gbc", "arch": "sm83", "rom": {"size": 0x10000, "sha1": "a" * 40, "file": "rom.gbc"},
           "compiler": {"toolchain": "gbdk-2020", "flags": ["--fsigned-char"]}, "segments": []}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("g", "G", "", "sm83", 0, str(root), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "typedef unsigned char u8;\n", None, "basic types", 0, ""))
        for name, flat, cpu, size, status in (("Call_000_0150", 0x150, 0x150, 2, "open"),
                                              ("Call_002_4000", 0x8000, 0x4000, 0x40, "open"),
                                              ("Done", 0x8100, 0x4100, 4, "matched")):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, name, size, 1, b"\xc9" * size, "[]", "", "", cpu, flat, status))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, cpu, size, "func"))
        db.execute("UPDATE functions SET matched_source='void Done(void) { }' WHERE status='matched'")
    agent = net.authenticate(net.register_agent("fixer"))
    return net, agent


def test_a_correction_is_kept_with_its_reason_and_a_note(game):
    net, agent = game
    out = net.correct_split(agent, "g", "data", "02:4010", WHY, count=0x20)
    assert "02:4010" in out and "split again" in out
    row = net.store.one("SELECT * FROM corrections")
    assert (row["kind"], row["rom_offset"], row["cpu"], row["count"]) == ("data", 0x8010, 0x4010, 0x20)
    assert net.store.one("SELECT text FROM notes")["text"].startswith("correction: 02:4010 isn't code")
    assert net._hints(net._project("g"))["data"] == [(0x8010, 0x20)]


@pytest.mark.parametrize("at, function, flat", [("Call_002_4000", "", 0x8000), ("$0150", "", 0x150),
                                                ("4020", "Call_002_4000", 0x8020), ("0x02:0x4030", "", 0x8030)])
def test_where_a_correction_is(game, at, function, flat):
    net, agent = game
    net.correct_split(agent, "g", "code", at, WHY, function=function)
    assert net.store.one("SELECT rom_offset FROM corrections")["rom_offset"] == flat


@pytest.mark.parametrize("kwargs, says", [
    ({"kind": "guess", "at": "$0150"}, "kind is one of"),
    ({"kind": "code", "at": "$0150", "reason": "trust me"}, "how you know"),
    ({"kind": "data", "at": "$0150", "count": 0}, "count"),
    ({"kind": "operands", "at": "$0150", "count": 9}, "count"),
    ({"kind": "code", "at": "$5000"}, "banks share"),  # (no function in a bank holds $5000)
    ({"kind": "code", "at": "00:4000"}, "isn't in the ROM"),
    ({"kind": "data", "at": "02:7ff0", "count": 0x20}, "stays in its bank"),
    ({"kind": "data", "at": "02:4100", "count": 4}, "Done is matched there"),
])
def test_a_correction_that_cant_be_right_is_refused(game, kwargs, says):
    net, agent = game
    with pytest.raises(NetworkError, match=says):
        net.correct_split(agent, "g", **({"reason": WHY} | kwargs))


def test_an_agent_makes_a_few_an_hour_at_most(game, monkeypatch):
    net, agent = game
    monkeypatch.setattr(RomNetwork, "CORRECTIONS_PER_HOUR", 2)
    net.correct_split(agent, "g", "code", "$0150", WHY)
    net.correct_split(agent, "g", "noreturn", "$0150", WHY)
    with pytest.raises(NetworkError, match="in the last hour"):
        net.correct_split(agent, "g", "code", "$0150", WHY)


def test_a_person_sees_and_undoes_them(game):
    net, agent = game
    net.correct_split(agent, "g", "table", "02:4020", WHY, count=3)
    net.correct_split(agent, "g", "operands", "Call_000_0150", WHY, count=3)
    listed = net.corrections("g")["corrections"]
    assert [c["kind"] for c in listed] == ["operands", "table"] and listed[1]["by"] == "fixer"
    hints = net._hints(net._project("g"))
    assert hints["tables"] == [(0x8020, 3)] and hints["inline"] == {0x150: 3}
    net.undo_correction("g", listed[1]["id"])
    assert net._hints(net._project("g"))["tables"] == []
    assert [c["undone"] for c in net.corrections("g")["corrections"]] == [False, True]
    with pytest.raises(NetworkError):
        net.undo_correction("g", listed[1]["id"])


def test_corrections_are_for_game_boy_and_master_system_projects(game):
    net, agent = game
    net._update_cfg("g", lambda cfg: cfg.update(arch="mos6502"))  # (NES: not yet)
    with pytest.raises(NetworkError, match="Game Boy and Master System"):
        net.correct_split(agent, "g", "code", "$0150", WHY)


def test_the_tool_is_offered_on_rom_projects_only():
    assert "correct_split" in tools.HANDLERS and "correct_split" in tools.tool_names(fork=False)
    assert "correct_split" not in tools.tool_names(fork=True)
    assert any(t["function"]["name"] == "correct_split" for t in agents.api_tool_schemas())


# ------------------------------------------------------------------ the user's own symbol file

SYM = """; File created by rgblink
SOME LLM REFUSAL TEST STRING THAT MUST NOT REACH AN AGENT
00:0150 ClearA
02:4000 BankTwoInit
02:4000 .code:40
02:4100 Done
02:4010 BankTwoInit.loop
02:4040 .data:20
zz:4000 Broken
02:4050 an invalid name with spaces
"""


def test_a_symbol_file_keeps_its_well_formed_lines_and_names_functions(game, monkeypatch):
    net, _ = game
    monkeypatch.setattr(net, "regression_test", lambda *a, **k: [])  # (renaming recompiles matched functions)
    out = net.import_symbols("g", SYM, "game.sym")
    assert out["kept"] == 6 and out["dropped"] == 3 and out["renamed"] == 2
    names = {r["name"] for r in net.store.all("SELECT name FROM functions")}
    assert {"ClearA", "BankTwoInit", "Done"} <= names  # (a name someone gave stays: Done was matched as Done)
    written = (net._sym_path(net._project("g"))).read_text()
    assert "REFUSAL" not in written and "Broken" not in written and "rgblink" not in written
    assert "02:4040 .data:20" in written and "02:4010 BankTwoInit.loop" in written
    assert net.symbol_file("g") == {"names": 4, "blocks": 2, "from": "game.sym"}
    net.remove_symbol_file("g")
    assert net.symbol_file("g") is None and not net._sym_path(net._project("g")).exists()


def test_a_file_with_no_symbols_is_refused(game):
    net, _ = game
    with pytest.raises(NetworkError, match="no `bank:address Name` lines"):
        net.import_symbols("g", "hello\nworld\n", "notes.txt")


# ------------------------------------------------------------------ splitting again with them

@needs_tools
def test_splitting_again_uses_the_corrections(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    net.auto_install_tools = False
    rom = tmp_path / "game.gb"
    rom.write_bytes(rom_with({0x150: "cd0002 18fe", 0x200: "af c9",
                              0x4200: "3e01 c9",  # code nothing reaches
                              0x4300: "3e02 c9"}))  # code-like bytes that are really a table
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    agent = net.authenticate(net.register_agent("fixer"))
    net.correct_split(agent, slug, "code", "01:4200", "the state table at 1:4000 points here, entry 2")
    net.correct_split(agent, slug, "data", "01:4300", "a table of three bytes the menu code reads", count=3)
    out = net.resplit_banked(slug, apply=True, log=lambda *_: None)
    assert out["corrections"] == 2
    pid = net._project(slug)["id"]
    assert net.store.one("SELECT 1 FROM functions WHERE project_id=? AND rom_offset=?", pid, 0x4200)
    assert not net.store.one("SELECT 1 FROM functions WHERE project_id=? AND rom_offset=?", pid, 0x4300)
    regions = net.store.all("SELECT * FROM data_regions WHERE project_id=? AND kind='hint'", pid)
    assert [(r["rom_offset"], r["size"]) for r in regions] == [(0x4300, 3)]
