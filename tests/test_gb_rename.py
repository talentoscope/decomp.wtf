"""rename_symbol on a Game Boy project: every switchable bank is mapped at 0x4000-0x7FFF, so two banks'
functions can share a CPU address (vram). A rename must touch only the function it names."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "gb.db", tmp_path / "work")
    cfg = {"kind": "rom", "platform": "gb", "arch": "sm83", "big_endian": False, "gp": 0,
           "compiler": {"toolchain": "", "flags": []}}
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'gbgame', 'gbgame', 'gb fixture', 'sm83', 0, ?, ?)",
                   (str(tmp_path / "work"), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'void func_001_4000(void);', NULL, 'fixture', 0, '')")
        for fid, bank in ((1, 1), (2, 2)):  # the same CPU address in two banks
            name = f"func_{bank:03d}_4000"
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                       "unit, vram, rom_offset) VALUES(?,1,?,1,1,?,'[]','','asm',?,?,?)",
                       (fid, name, b"\xc9", f"bank{bank:03d}", 0x4000, bank * 0x4000))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(1,?,?,1,'func',NULL)",
                       (name, 0x4000))
    return n


def test_renaming_a_banked_function_leaves_the_other_banks_function_alone(net):
    alice = net.authenticate(net.register_agent("alice"))
    out = net.rename_symbol(alice, "gbgame", "func_001_4000", "UpdatePlayer")
    assert out.startswith("✅")
    names = {r["id"]: r["name"] for r in net.store.all("SELECT id, name FROM functions WHERE project_id=1")}
    assert names == {1: "UpdatePlayer", 2: "func_002_4000"}
    syms = {r["name"] for r in net.store.all("SELECT name FROM symbols WHERE project_id=1")}
    assert syms == {"UpdatePlayer", "func_002_4000"}


def test_renaming_the_other_banks_function_works_too(net):
    alice = net.authenticate(net.register_agent("alice"))
    assert net.rename_symbol(alice, "gbgame", "func_002_4000", "DrawMap").startswith("✅")
    names = {r["id"]: r["name"] for r in net.store.all("SELECT id, name FROM functions WHERE project_id=1")}
    assert names == {1: "func_001_4000", 2: "DrawMap"}


def test_a_bank_cpu_name_resolves_to_its_cpu_address():
    from decompnet.rom.symbols import SymbolTable

    assert SymbolTable().resolve("func_003_4abc") == 0x4ABC
    assert SymbolTable().resolve("func_80012340") == 0x80012340


def test_both_banks_names_resolve_and_an_old_automatic_name_still_renames(net):
    """Two banks' functions at one CPU address: both names resolve (a call by name compiles, the rename's check has
    both), and a name from an older listing finds its function by its bank."""
    syms = net.symbols(1)
    assert syms.resolve("func_001_4000") == syms.resolve("func_002_4000") == 0x4000
    alice = net.authenticate(net.register_agent("alice"))
    assert net.rename_symbol(alice, "gbgame", "func_002_4000", "DrawMap").startswith("✅")
    assert net.rename_symbol(alice, "gbgame", "func_002_4000", "DrawWorldMap").startswith("✅")  # the old name
    names = {r["id"]: r["name"] for r in net.store.all("SELECT id, name FROM functions WHERE project_id=1")}
    assert names == {1: "func_001_4000", 2: "DrawWorldMap"}
    assert net.rename_symbol(alice, "gbgame", "func_002_4000", "DrawWorldMap").startswith("✅ func_002_4000 is already")


def test_read_data_takes_the_addresses_these_games_write_and_says_which_bank(net, tmp_path):
    rom = bytearray(0xC000)
    rom[0x4100:0x4104] = b"\x11\x22\x33\x44"  # bank 1 at $4100
    rom[0x8100:0x8104] = b"\xaa\xbb\xcc\xdd"  # bank 2 at $4100
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "game.gb").write_bytes(bytes(rom))
    cfg = json.loads(net.project_row("gbgame")["config"])
    cfg["rom"] = {"file": "game.gb", "size": len(rom)}
    cfg["segments"] = [{"name": f"bank{b:03d}", "rom_start": b * 0x4000, "rom_end": (b + 1) * 0x4000,
                        "vram": 0 if b == 0 else 0x4000} for b in range(3)]
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1", (json.dumps(cfg),))
    for what in ("0x4100", "$4100", "4100"):
        out = net.read_data("gbgame", what, 16)
        assert "11 22 33 44" in out and "bank001" in out and 'read_data(what="NN:4100")' in out, what
    assert "aa bb cc dd" in net.read_data("gbgame", "02:4100", 16)
    assert "aa bb cc dd" in net.read_data("gbgame", "$4100", 16, unit="bank002")
    assert "RAM" in net.read_data("gbgame", "0xC0A0", 16)
