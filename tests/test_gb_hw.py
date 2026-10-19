"""The Game Boy's hardware by name in listings (rom/gb_hw.py, from hardware.inc 6.0, vendored)."""

from __future__ import annotations

import json

from decompnet.rom import gb_hw


def test_registers_and_areas_come_from_hardware_inc():
    regs = gb_hw.registers()
    assert regs[0xFF40] == "rLCDC" and regs[0xFF4D] == "rSPD" and regs[0xFF70] == "rWBK" and regs[0xFFFF] == "rIE"
    assert gb_hw.area(0x9800) == "TILEMAP0" and gb_hw.area(0x9C00) == "TILEMAP1" and gb_hw.area(0xC000) is None
    assert gb_hw.mapper_registers("mbc5")[0x2000] == "rROMB0" and gb_hw.mapper_registers("mbc1")[0x2000] == "rROMB"


def test_older_names_are_the_same_registers_by_their_6_0_names():
    old = gb_hw.old_names()
    assert (old["rKEY1"], old["rSVBK"], old["rP1"], old["rHDMA5"], old["rBCPS"]) == (
        "rSPD", "rWBK", "rJOYP", "rVDMA_LEN", "rBGPI")
    assert "def rKEY1 equ rSPD" in gb_hw.old_names_inc()


def test_a_listing_names_its_registers():
    listing = "\n".join(["    ldh [$ff00+$40], a", "0150  ldh ($ff00+$41), a", "    ld [$ff4d], a",
                         "    ld [$2000], a", "    ld hl, $9800", "    ld de, $ff47", "    ldh [rKEY1], a",
                         "    ld a, [$c0a2] ; a variable", "    ld a, ($c0a2)"])
    assert gb_hw.name_listing(listing, "mbc5").splitlines() == [
        "    ldh [rLCDC], a", "0150  ldh [rSTAT], a", "    ld [rSPD], a", "    ld [rROMB0], a",
        "    ld hl, $9800 ; TILEMAP0", "    ld de, $ff47 ; rBGP", "    ldh [rSPD], a",
        "    ld a, [$c0a2] ; a variable", "    ld a, [$c0a2]"]
    assert gb_hw.mapper_of("MBC5+RAM+battery") == "mbc5" and gb_hw.mapper_of("MBC1") == "mbc1"
    assert gb_hw.mapper_of("ROM only") == "mbc5"


def test_a_brief_shows_the_names(tmp_path):
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "gbc", "arch": "sm83", "rom": {"size": 0x8000, "mbc": "MBC5", "file": "rom.gbc"},
           "compiler": {"toolchain": "gbdk-2020", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("g", "G", "", "sm83", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, "Call_000_0150", 3, 2, bytes.fromhex("e040c9"), "[]",
                                                       "    ldh [$ff40], a\n    ret", "", 0x150, 0x150, "open"))
    fn = net.store.one("SELECT * FROM functions")
    assert "ldh [rLCDC], a" in net._asm_text(fn, net._project("g"))
