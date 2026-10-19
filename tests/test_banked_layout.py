"""Function boundaries on the banked lanes (here the Game Boy), and rooms on them: every switchable bank is shown at
0x4000-0x7FFF, so two banks' functions share CPU addresses. A function is cut or joined by its bytes, and a room
member's copy finds the host's function by ROM offset or name, never by a CPU address another bank also has."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

TWO_ROUTINES = bytes.fromhex("3e01c9" "3e02c9")  # ld a,1; ret | ld a,2; ret: the split took two for one
RUNS_ON = bytes.fromhex("3e01")                  # ld a,1 ... and on into the next: half of a routine
REST = bytes.fromhex("0602c9")                   # ld b,2; ret


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "gb.db", tmp_path / "work")
    cfg = {"kind": "rom", "platform": "gb", "arch": "sm83", "big_endian": False, "gp": 0, "rom": {"size": 0x10000},
           "compiler": {"toolchain": "gbdk-2020", "flags": []}}
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'gbgame', 'gbgame', 'gb fixture', 'sm83', 0, ?, ?)", (str(tmp_path / "work"), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, '', NULL, 'fixture', 0, '')")
    return n


def _add(net, name, flat, code, status="open", relocs="[]"):
    vram = flat if flat < 0x4000 else 0x4000 + flat % 0x4000
    with net.store.tx() as db:
        fid = db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, status, unit, vram, "
                         "rom_offset) VALUES(1,?,?,?,?,?,'',?,?,?,?)",
                         (name, len(code), 1, code, relocs, status, f"bank{flat // 0x4000:03d}", vram, flat)).lastrowid
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(1,?,?,?,'func')", (name, vram, len(code)))
    return fid


def _rows(net):
    return {r["name"]: r for r in net.store.all("SELECT * FROM functions WHERE project_id=1")}


def test_a_function_that_is_two_routines_is_cut_at_the_second(net):
    _add(net, "func_001_4000", 0x4000, TWO_ROUTINES)
    _add(net, "func_002_4003", 0x8003, b"\xc9")  # another bank's function at the CPU address the cut makes
    alice = net.authenticate(net.register_agent("alice"))
    with pytest.raises(NetworkError, match="middle of an instruction"):
        net.split_function(alice, "gbgame", "func_001_4000", "0x4001")
    with pytest.raises(NetworkError, match="isn't inside"):
        net.split_function(alice, "gbgame", "func_001_4000", "0x4006")
    out = net.split_function(alice, "gbgame", "func_001_4000", "4003")
    assert out.startswith("✅ split") and "Set aside" not in out
    rows = _rows(net)
    head, tail = rows["func_001_4000"], rows["func_001_4003"]  # the bank's own name, not the other bank's
    assert bytes(head["target"]) == bytes.fromhex("3e01c9") and head["size"] == 3 and head["n_insns"] == 2
    assert bytes(tail["target"]) == bytes.fromhex("3e02c9") and tail["rom_offset"] == 0x4003 and tail["vram"] == 0x4003
    assert tail["status"] == "open" and "ld a, $02" in tail["asm"] and tail["unit"] == "bank001"
    assert rows["func_002_4003"]["size"] == 1  # untouched
    with pytest.raises(NetworkError, match="not a free C identifier"):
        net.split_function(alice, "gbgame", "func_001_4003", "0x4005", "func_001_4000")


def test_half_a_routine_is_joined_with_its_rest_unless_the_code_says_they_are_two(net):
    _add(net, "func_001_4000", 0x4000, TWO_ROUTINES[:3])
    _add(net, "func_001_4003", 0x4003, TWO_ROUTINES[3:])
    _add(net, "func_001_4010", 0x4010, RUNS_ON, status="asm")  # set aside: it runs on
    _add(net, "func_001_4012", 0x4012, REST)
    _add(net, "func_001_4100", 0x4100, REST)
    alice = net.authenticate(net.register_agent("alice"))
    with pytest.raises(NetworkError, match="They're functions of their own"):
        net.merge_function(alice, "gbgame", "func_001_4000")  # it returns before the next one starts
    with pytest.raises(NetworkError, match="nothing to join"):
        net.merge_function(alice, "gbgame", "func_001_4100")
    net.add_note(alice, "gbgame", "the rest of a routine", "func_001_4012")
    net.claim_row(alice, "gbgame", "func_001_4012")
    out = net.merge_function(alice, "gbgame", "func_001_4012")  # the half an agent is given: the one before runs on into it
    assert out.startswith("✅ merged: func_001_4012 is part of func_001_4010") and 'claim_function("func_001_4010")' in out
    rows = _rows(net)
    joined = rows["func_001_4010"]
    assert "func_001_4012" not in rows and bytes(joined["target"]) == RUNS_ON + REST and joined["size"] == 5
    assert joined["status"] == "open" and "asm_code" not in json.loads(joined["meta"] or "{}")  # whole now: open again
    assert net.store.one("SELECT function_id FROM notes WHERE text='the rest of a routine'")["function_id"] == joined["id"]
    assert net.store.one("SELECT 1 FROM symbols WHERE name='func_001_4012'") is None


def test_a_function_something_else_calls_is_never_joined_onto_another(net):
    _add(net, "func_001_4010", 0x4010, RUNS_ON)
    _add(net, "func_001_4012", 0x4012, REST)
    _add(net, "func_001_4100", 0x4100, bytes.fromhex("cd1240c9"), relocs=json.dumps([[0, 10, 0x4012]]))
    _add(net, "func_002_4100", 0x8100, bytes.fromhex("cd1240c9"), relocs=json.dumps([[0, 10, 0x4012]]))
    alice = net.authenticate(net.register_agent("alice"))
    with pytest.raises(NetworkError, match="called or jumped to from func_001_4100: it's a function of its own"):
        net.merge_function(alice, "gbgame", "func_001_4010")  # (bank 2's call to 0x4012 is bank 2's code: not it)


def test_a_room_member_applies_the_hosts_work_to_the_right_banks_function(net):
    one, two = _add(net, "func_001_4000", 0x4000, b"\xc9"), _add(net, "func_002_4000", 0x8000, b"\xc9")
    # an older host: the CPU address alone is shared by two banks, so it's not applied anywhere
    net.apply_remote_match("gbgame", 0x4000, "void f(void) {}", "host")
    net.apply_remote_best("gbgame", 0x4000, 50, "void f(void) {}")
    assert {r["status"] for r in _rows(net).values()} == {"open"} and {r["best_score"] for r in _rows(net).values()} == {None}
    net.apply_remote_best("gbgame", 0x4000, 40, "void g(void) {}", name="func_002_4000", rom_offset=0x8000, size=1)
    net.apply_remote_match("gbgame", 0x4000, "void func_001_4000(void) {}", "host", name="func_001_4000",
                           rom_offset=0x4000, size=1)
    rows = {r["id"]: r for r in _rows(net).values()}
    assert rows[one]["status"] == "matched" and rows[two]["status"] == "open" and rows[two]["best_score"] == 40
    net.apply_remote_best("gbgame", 0x4000, 30, "void h(void) {}", name="func_002_4000", rom_offset=0x8000, size=9)
    assert net._function("func_002_4000", "gbgame")["best_score"] == 40  # another extent on the host: other bytes
    net.apply_remote_note("gbgame", 7, 0x4000, "bank 2's", "host", 1.0, name="func_002_4000", rom_offset=0x8000)
    assert net.store.one("SELECT function_id FROM notes WHERE text=?", "bank 2's")["function_id"] == two


def test_a_room_member_follows_the_hosts_names_cuts_and_joins_by_rom_offset(net):
    _add(net, "func_001_4000", 0x4000, TWO_ROUTINES)
    _add(net, "func_001_4010", 0x4010, RUNS_ON, status="asm")
    _add(net, "func_001_4012", 0x4012, REST)
    _add(net, "func_002_4000", 0x8000, b"\xc9")
    host = [{"name": "func_001_4000", "vram": 0x4000, "size": 3, "rom_offset": 0x4000},
            {"name": "LoadTwo", "vram": 0x4003, "size": 3, "rom_offset": 0x4003},
            {"name": "func_001_4010", "vram": 0x4010, "size": 5, "rom_offset": 0x4010},
            {"name": "DrawMap", "vram": 0x4000, "size": 1, "rom_offset": 0x8000}]
    symbols = [{"name": "LoadTwo", "address": 0x4003, "size": 3, "kind": "func"},
               {"name": "DrawMap", "address": 0x4000, "size": 1, "kind": "func"}]
    net.apply_remote_layout("gbgame", host, symbols)
    rows = _rows(net)
    assert set(rows) == {"func_001_4000", "LoadTwo", "func_001_4010", "DrawMap"}
    assert rows["LoadTwo"]["rom_offset"] == 0x4003 and rows["func_001_4000"]["size"] == 3
    assert rows["func_001_4010"]["size"] == 5 and rows["func_001_4010"]["status"] == "open"
    assert rows["DrawMap"]["rom_offset"] == 0x8000  # bank 2's, by its ROM offset; bank 1's 0x4000 keeps its name
