"""A Game Boy project exported as RGBDS assembly: the project's names and notes in the disassembly, its matched C for
reference, and a build that remakes the game from the user's copy, byte for byte."""

import json
import subprocess
import sys

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.rom import gb_asm

from .test_gb import make_rom

pytestmark = pytest.mark.skipif(toolchains.installed("mgbdis") is None or toolchains.installed("rgbds") is None,
                                reason="needs mgbdis and RGBDS (Setup installs them)")

# at 0x150: call a routine that switches banks the way games do, then loop; then padding
CODE = bytes.fromhex("cd5801" "c35001" "0000" "3e01e09bea0020c9") + b"\xff" * 16


@pytest.fixture
def exported(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    slug = net.import_rom(make_rom(size=0x8000, cgb=0xC0, code=CODE), confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", pid, 0x158)
    with net.store.tx() as db:  # what agents do: a name, a note, a match
        db.execute("UPDATE functions SET name='SetBank1', status='matched', matched_source=? WHERE id=?",
                   ("void SetBank1(void) { *(volatile unsigned char *)0xff9b = 1; }", fn["id"]))
        db.execute("UPDATE symbols SET name='SetBank1' WHERE project_id=? AND name=?", (pid, fn["name"]))
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                   (pid, fn["id"], None, "switches to bank 1 the way the game always does: $ff9b, then $2000", 0))
    res = net.export_rom(slug, log=lambda *_: None)
    return net, slug, res


def test_the_export_rebuilds_the_game_byte_for_byte(exported):
    net, slug, res = exported
    assert res["identical"], res["log"]
    cfg = net.cfg(net.project_row(slug))
    assert cfg["export"]["identical"] is True and cfg["export"]["matched"] == 1
    assert net.overview(slug)["exportable"]


def test_the_disassembly_has_the_projects_names_notes_and_c(exported):
    net, slug, res = exported
    from pathlib import Path
    out = Path(res["path"])
    assert "00:0158 SetBank1" in (out / "baserom.sym").read_text()
    bank0 = (out / "build" / "disassembly" / "bank_000.asm").read_text()
    assert "call SetBank1" in bank0
    lines = bank0.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("SetBank1:"))
    above = "\n".join(lines[at - 3:at])
    assert "; Matched in C: c/SetBank1.c" in above and "switches to bank 1" in above
    assert "0xff9b = 1" in (out / "c" / "SetBank1.c").read_text() and (out / "c" / "context.h").exists()


def test_the_tree_keeps_the_game_out_of_git(exported):
    net, slug, res = exported
    from pathlib import Path
    out = Path(res["path"])
    ignored = (out / ".gitignore").read_text().split()
    assert "build/" in ignored and "baserom.gbc" in ignored and "baserom.sym" not in ignored
    tracked = [p for p in out.rglob("*") if p.is_file() and not any(part in ("build",) for part in p.relative_to(out).parts)
               and p.name != "baserom.gbc"]
    rom = (out / "baserom.gbc").read_bytes()
    assert all(rom[0x150:0x160] not in p.read_bytes() for p in tracked)  # the game's code is in none of them


def test_the_build_refuses_another_game(exported):
    net, slug, res = exported
    from pathlib import Path
    out = Path(res["path"])
    (out / "baserom.gbc").write_bytes(make_rom(size=0x8000, cgb=0xC0, code=CODE.replace(b"\x3e\x01", b"\x3e\x02")))
    proc = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True)
    assert proc.returncode != 0 and "isn't the version" in proc.stderr
    assert json.loads((out / "build.json").read_text())["image"] == "baserom.gbc"


def test_a_name_mgbdis_has_no_label_for_is_left_out_not_broken(tmp_path):
    """A function the project knows at an address mgbdis decodes mid-instruction (the built-in split's, say): mgbdis
    would use the name and never define it, and the build wouldn't link. It's left out, and the export says so."""
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    slug = net.import_rom(make_rom(size=0x8000, cgb=0xC0, code=CODE), confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    with net.store.tx() as db:  # inside the call at 0x150 (cd 58 01): no instruction starts at 0x151
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, "MidCall", 2, 1, b"\x58\x01", "[]", "", "bank000", 0x151, 0x151, "asm"))
    res = net.export_rom(slug, log=lambda *_: None)
    assert res["identical"], res["log"]
    assert [n for n, _ in res["fallback"]] == ["MidCall"]


SET_BANK = """; Maps bank 1 the way the game always does: the bank it keeps in HRAM first, then the mapper.
; In: nothing. Out: A = 1. Maps bank 1 at $4000.
SetBank1:
    ld a, 1
    ldh [hCurrentBank], a
    ld [rROMB0], a
    ret
"""


def _documented(tmp_path, text=SET_BANK):
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    slug = net.import_rom(make_rom(size=0x8000, cgb=0xC0, code=CODE), confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", pid, 0x158)
    with net.store.tx() as db:  # documented in assembly, its DEF name the project's
        db.execute("UPDATE functions SET name='SetBank1', status='documented', matched_source=? WHERE id=?", (text, fn["id"]))
        db.execute("UPDATE symbols SET name='SetBank1' WHERE project_id=? AND name=?", (pid, fn["name"]))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, "hCurrentBank", 0xFF9B, 0, "data"))
    return net, slug


@pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")
def test_a_documented_routine_goes_in_place_of_its_bytes(tmp_path):
    from pathlib import Path
    net, slug = _documented(tmp_path)
    res = net.export_rom(slug, log=lambda *_: None)
    assert res["identical"], res["log"]
    assert (res["documented"], res["left_out"]) == (1, [])
    out = Path(res["path"])
    assert (out / "documented" / "SetBank1.asm").read_text() == SET_BANK
    assert "DEF hCurrentBank EQU $FF9B" in (out / "documented" / "names.inc").read_text()
    assert (out / "include" / "hardware.inc").exists() and "documented/" in (out / "README.md").read_text()
    bank0 = (out / "build" / "disassembly" / "bank_000.asm").read_text()
    assert "; documented in assembly: documented/SetBank1.asm" in bank0 and "    ldh [hCurrentBank], a" in bank0
    assert "call SetBank1" in bank0  # its caller names it (mgbdis wrote its bytes as data)
    assert net.cfg(net.project_row(slug))["export"]["documented"] == 1
    plain = subprocess.run([sys.executable, "build.py", "--no-documented"], cwd=out, capture_output=True, text=True)
    assert plain.returncode == 0 and "identical" in plain.stdout


@pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")
def test_a_documented_routine_that_no_longer_passes_stays_as_mgbdis_has_it(tmp_path):
    """Judged again with the project's names as they are now: one a name has changed under is left out, and said."""
    net, slug = _documented(tmp_path)
    pid = net.project_row(slug)["id"]
    with net.store.tx() as db:  # a name it uses, gone
        db.execute("DELETE FROM symbols WHERE project_id=? AND name='hCurrentBank'", (pid,))
    res = net.export_rom(slug, log=lambda *_: None)
    assert res["identical"], res["log"]
    assert res["documented"] == 0 and [n for n, _ in res["left_out"]] == ["SetBank1"]
    assert "hCurrentBank" in res["left_out"][0][1]


@pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")
def test_renaming_a_name_rewrites_the_documented_routines_that_use_it(tmp_path):
    net, slug = _documented(tmp_path, SET_BANK.replace("    ld [rROMB0], a\n", ".hCurrentBank:\n    ld [rROMB0], a\n"))
    agent = net.authenticate(net.register_agent("namer"))
    assert net.rename_symbol(agent, slug, "hCurrentBank", "hMappedBank").startswith("✅")
    text = net.store.one("SELECT matched_source FROM functions WHERE name='SetBank1'")["matched_source"]
    assert "ldh [hMappedBank], a" in text and ".hCurrentBank:" in text  # (a local label of the same name is its own)
