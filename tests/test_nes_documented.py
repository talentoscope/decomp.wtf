"""The assembly track on an NES project: a hand-written routine is claimed with a brief and a draft in ca65, judged by
rom/nes_asm.py, and once accepted kept as documented assembly under its label's name; and what neither SDCC nor
cc65 writes is set aside for it (handwritten.mos6502_reason)."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.handwritten import mos6502_reason
from decompnet.rom import nes_asm
from tests.test_nes_asm import CODE, FLAT, GOOD, SOUND

HAND = json.dumps({"asm_code": "reads y before setting it (sty $10): neither C compiler passes an argument in y"})


@pytest.fixture
def game(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "nes", "arch": "mos6502",
           "rom": {"size": 16 + 0x20000, "file": "rom.nes", "mapper": 4, "prg_offset": 16, "prg_size": 0x20000,
                   "bank_size": 0x2000}, "compiler": {"toolchain": "gbdk-2020", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("n", "N", "", "mos6502", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", 0, ""))
        for name, flat, cpu, code, status, meta in (("func_005_8a10", FLAT, 0x8A10, CODE, "asm", HAND),
                                                     ("UpdateSound", SOUND, 0xC123, b"\x60", "open", None)):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status,meta) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, name, len(code), 1, code, "[]", "", "", cpu, flat, status, meta))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, cpu, len(code), "func"))
        for name, address in (("zMenuOpen", 0x10), ("wMenuBank", 0x0300)):
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, address, 0, "data"))
    return net, net.authenticate(net.register_agent("writer"))


@pytest.mark.skipif(not nes_asm.available(), reason="needs cc65")
def test_an_nes_routine_is_documented_through_the_track(game):
    net, agent = game
    assert {r["name"]: r for r in net.function_map("n")}["func_005_8a10"].get("track")
    brief = net.claim(agent, "n", track="asm")
    assert "CLAIMED (assembly track): n/func_005_8a10" in brief and "ca65" in brief and "sta PPUCTRL" in brief
    out = net.compile_and_diff(agent, "func_005_8a10", GOOD, "n")
    assert out.startswith("✅ DOCUMENTED: StartMenu"), out
    fn = net.store.one("SELECT * FROM functions WHERE rom_offset=?", FLAT)
    assert (fn["status"], fn["name"]) == ("documented", "StartMenu")
    assert net.function_diff("n", "StartMenu")["asm"]["ok"]


@pytest.mark.parametrize("code, says", [
    ("9002 60 60", "reads a flag before setting it"),     # bcc +2 · rts · rts
    ("68 a8 60", "before any push"),                       # pla · tay · rts
    ("8410 60", "reads y before setting it"),              # sty $10 · rts
    ("b90003 60", "reads y before setting it"),            # lda $0300,y · rts
    ("f8 a900 60", "decimal mode"),                        # sed · lda #0 · rts
    ("a2ff 9a 60", "stack pointer"),                       # ldx #$ff · txs · rts
])
def test_what_neither_c_compiler_writes(code, says):
    why = mos6502_reason(bytes.fromhex(code.replace(" ", "")), 0xC000)
    assert why and says in why, why


@pytest.mark.parametrize("code", [
    "18 6901 60",          # clc · adc #1 · rts
    "a000 b110 60",        # ldy #0 · lda ($10),y · rts
    "48 68 60",            # pha · pla · rts
    "8d0003 60",           # sta $0300 · rts
])
def test_c_shapes_stay_on_the_c_track(code):
    assert mos6502_reason(bytes.fromhex(code.replace(" ", "")), 0xC000) is None
