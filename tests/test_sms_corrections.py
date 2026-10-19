"""Corrections to a Master System game's split (as on the Game Boy): agents say what the split got wrong about its
code and data, and splitting the game again uses them: a function nothing reaches, bytes that aren't code, a table
of handlers, a routine that reads the bytes after its call, one that never returns. Places are a bank and the address
it runs at. A symbol file the user chooses names functions, but not after its own automatic names."""

from __future__ import annotations

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

from .test_sms import make_rom

WHY = "the menu code at 00:0100 reads these with ld a,(hl) in a loop"
CODE = {
    0x0000: bytes.fromhex("c30001"),
    0x0100: bytes.fromhex("cd0003" "c3"  # call $0300 · db $c3   (the byte it reads: as code, `jp $053e`)
                          "3e05" "32ffff" "cd0080"  # ld a,5 · ld ($ffff),a · call $8000   (bank 5)
                          "cd0004"  # call $0400   (it never comes back: a correction says so)
                          "3e06" "32ffff" "cd0080" "18fe"),  # ld a,6 · ... · call $8000   (bank 6: not reached)
    0x0300: bytes.fromhex("e1" "23" "e5" "c9"),  # pop hl · inc hl · push hl · ret   (steps over its operand)
    0x0400: bytes.fromhex("c9"),
    0x0500: bytes.fromhex("0006" "2006"),  # a table of two handlers nothing reads
    0x0600: bytes.fromhex("3e01c9"), 0x0620: bytes.fromhex("3e02c9"),
    0x0700: bytes.fromhex("3e03c9"),  # code nothing reaches
    0x0800: bytes.fromhex("cd0009"),  # three bytes of a table that read like `call $0900`
    0x0900: bytes.fromhex("3e04c9"),
    0x14000: bytes.fromhex("3e07c9"), 0x18000: bytes.fromhex("3e08c9"),
}


@pytest.fixture
def game(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    net.auto_install_tools = False
    rom = tmp_path / "game.sms"
    rom.write_bytes(make_rom(size=0x20000, code=CODE))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    return net, slug, net.authenticate(net.register_agent("fixer"))


def _starts(net, slug) -> set[int]:
    pid = net._project(slug)["id"]
    return {r["rom_offset"] for r in net.store.all("SELECT rom_offset FROM functions WHERE project_id=?", pid)}


def test_splitting_again_uses_the_corrections(game):
    net, slug, agent = game
    before = _starts(net, slug)
    assert 0x18000 in before and 0x14000 not in before and 0x0900 in before  # (what the split got wrong)
    net.correct_split(agent, slug, "operands", "00:0300", "it pops its return address, steps over one byte, pushes it",
                      count=1)
    net.correct_split(agent, slug, "noreturn", "00:0400", "it's the reset routine: it jumps to the start, never back")
    net.correct_split(agent, slug, "table", "00:0500", WHY, count=2)
    net.correct_split(agent, slug, "code", "00:0700", "the state table at 00:0500 points here when the game starts")
    net.correct_split(agent, slug, "data", "00:0800", WHY, count=3)
    out = net.resplit_banked(slug, apply=True, log=lambda *_: None)
    assert out["corrections"] == 5
    after = _starts(net, slug)
    assert 0x14000 in after and 0x18000 not in after  # past the operand byte, and not past the call that doesn't return
    assert {0x0600, 0x0620, 0x0700} <= after and 0x0800 not in after and 0x0900 not in after
    pid = net._project(slug)["id"]
    regions = {(r["rom_offset"], r["size"], r["kind"]) for r in net.store.all(
        "SELECT * FROM data_regions WHERE project_id=?", pid)}
    assert {(0x0800, 3, "hint"), (0x0103, 1, "inline")} <= regions
    caller = net.store.one("SELECT asm FROM functions WHERE project_id=? AND rom_offset=?", pid, 0x0100)["asm"]
    assert "db $c3" in caller and "jp $053e" not in caller


def test_a_place_is_a_bank_and_the_address_it_runs_at(game):
    net, slug, agent = game
    proj = net._project(slug)
    assert net._banked_at(proj, "05:8123") == (5 * 0x4000 + 0x123, 0x8123)  # slot 2
    assert net._banked_at(proj, "01:4010") == (0x4010, 0x4010)  # slot 1
    assert net._banked_at(proj, "04:1234") == (4 * 0x4000 + 0x1234, 0x1234)  # slot 0, paged
    for bad in ("00:8123", "05:C123", "05:0100"):
        with pytest.raises(NetworkError, match="isn't in the ROM"):
            net._banked_at(proj, bad)


def test_a_symbol_file_names_functions_but_not_after_its_automatic_names(game):
    net, slug, agent = game
    out = net.import_symbols(slug, "[labels]\n00:0700 StartGame\n00:0600 _LABEL_600_\n", "game.sym")
    assert out["kept"] == 2 and out["renamed"] <= 1
    pid = net._project(slug)["id"]
    names = {r["rom_offset"]: r["name"] for r in net.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
    assert not any(n.startswith("_LABEL_") for n in names.values())
    net.correct_split(agent, slug, "code", "00:0700", "the state table at 00:0500 points here when the game starts")
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    names = {r["rom_offset"]: r["name"] for r in net.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
    assert names[0x0700] == "StartGame"
