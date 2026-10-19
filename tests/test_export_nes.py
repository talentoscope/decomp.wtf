"""An NES project exported as ca65/ld65 assembly: the project's names and notes in a disassembly made from the user's
copy at build time (the header and graphics included from it), routines documented in assembly in place of their
bytes, and a build that remakes the game byte for byte. The tree holds the project's work, never the game's bytes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import nes_asm

from .test_nes import make_rom

pytestmark = pytest.mark.skipif(not nes_asm.available(), reason="needs cc65")

CODE = {
    0x0000: bytes.fromhex("2010c0" "a5f0" "8d0020" "4c03c0"),  # jsr ShowMenu · lda $f0 · sta $2000 · jmp $c003
    0x0010: bytes.fromhex("a510" "f005" "a980" "8d0120" "60"),  # ShowMenu: lda $10 · beq +5 · lda #$80 · sta $2001 · rts
}
SHOW = """; Turns the background on when the menu flag is set. In: nothing.
; Out: A trashed. Called once a frame from the main loop.
ShowMenu:
    lda zMenuOpen
    beq @done
    lda #$80
    sta PPUMASK
@done:
    rts
"""


@pytest.fixture
def exported(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    rom = tmp_path / "game.nes"
    rom.write_bytes(make_rom(prg_units=1, chr_units=1, code=CODE, vectors=(0xC000, 0xC000, 0xC000)))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", pid, 0xC010)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET name='ShowMenu', status='documented', matched_source=? WHERE id=?", (SHOW, fn["id"]))
        db.execute("UPDATE symbols SET name='ShowMenu' WHERE project_id=? AND name=?", (pid, fn["name"]))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, "zMenuOpen", 0x10, 0, "data"))
    res = net.export_rom(slug, log=lambda *_: None)
    return net, slug, res, Path(res["path"])


def test_the_export_rebuilds_the_game_with_its_documented_routine(exported):
    net, slug, res, out = exported
    assert res["identical"], res["log"]
    assert (res["documented"], res["left_out"]) == (1, [])
    assert net.overview(slug)["exportable"]
    bank = (out / "build" / "disassembly" / "bank_000.s").read_text()
    assert "jsr ShowMenu" in bank and "; documented in assembly: documented/ShowMenu.s" in bank
    assert "    sta PPUCTRL" in bank and "    sta PPUMASK" in bank and '.org $C000' in bank
    assert "zMenuOpen = $0010" in (out / "documented" / "names.inc").read_text()
    game = (out / "build" / "disassembly" / "game.s").read_text()
    assert '.incbin "baserom.nes", 0, 16' in game and '.segment "CHR"' in game
    plain = subprocess.run([sys.executable, "build.py", "--no-documented"], cwd=out, capture_output=True, text=True)
    assert plain.returncode == 0 and "identical" in plain.stdout, plain.stdout + plain.stderr


def test_the_tree_keeps_the_game_out_of_git(exported):
    net, slug, res, out = exported
    assert {"build/", "baserom.nes"} <= set((out / ".gitignore").read_text().split())
    tracked = [p for p in out.rglob("*") if p.is_file() and "build" not in p.relative_to(out).parts
               and p.name != "baserom.nes" and "__pycache__" not in p.parts]
    rom = (out / "baserom.nes").read_bytes()
    assert all(rom[16:30] not in p.read_bytes() for p in tracked)
    assert any(f["name"] == "ShowMenu" for f in json.loads((out / "layout.json").read_text())["functions"])


def test_the_build_refuses_another_game(exported):
    net, slug, res, out = exported
    (out / "baserom.nes").write_bytes(make_rom(prg_units=1, chr_units=1, code={**CODE, 0x10: bytes.fromhex("60")},
                                               vectors=(0xC000, 0xC000, 0xC000)))
    proc = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True)
    assert proc.returncode != 0 and "isn't the version" in proc.stderr
