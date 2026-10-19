"""The assembly track on a Master System project: a hand-written routine is claimed with a brief and a draft in WLA-DX,
judged by rom/sms_asm.py, and once accepted kept as documented assembly, its .DEFINE names the project's and its label
its name."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import sms_asm
from tests.test_sms_asm import CODE, GOOD

pytestmark = pytest.mark.skipif(not sms_asm.available(), reason="needs WLA-DX")
HAND = json.dumps({"asm_code": "reads hl before setting it: SDCC passes arguments in a, e, de and bc, never in hl"})
TEXT = ".DEFINE wMenuOpen $C0A2\n" + GOOD


@pytest.fixture
def game(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "sms", "arch": "z80", "rom": {"size": 0x20000, "file": "rom.sms"},
           "compiler": {"toolchain": "gbdk-2020", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("s", "S", "", "z80", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", 0, ""))
        for name, flat, cpu, code, status, meta in (("func_005_8a10", 0x14A10, 0x8A10, CODE, "asm", HAND),
                                                     ("FillNameTable", 0x0F31, 0x0F31, b"\xc9", "open", None)):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status,meta) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, name, len(code), 1, code, "[]", "", "bank005", cpu, flat,
                                                           status, meta))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, cpu, len(code), "func"))
    from .test_sms import make_rom
    (tmp_path / "rom.sms").write_bytes(make_rom(size=0x20000, code={0x14A10: CODE, 0x0F31: b"\xc9"}))
    net.sms.configure("s", "sega", "annotated-original", "The authored fixture specifies Sega cartridge mapping.")
    return net, net.authenticate(net.register_agent("writer"))


def test_a_master_system_routine_is_on_the_track_with_a_wla_dx_brief(game):
    net, agent = game
    row = {r["name"]: r for r in net.function_map("s")}["func_005_8a10"]
    assert row.get("track")
    brief = net.claim(agent, "s", track="asm")
    assert "CLAIMED (assembly track): s/func_005_8a10" in brief and "WLA-DX" in brief
    assert "out (PortVDPControl), a" in brief and "ld (MapperSlot2), a" in brief  # (the draft names the hardware)
    detail = net.function_detail("s", "func_005_8a10")
    assert detail["source_kind"] == "asm_draft" and "@l_8a25:" in detail["source"]


def test_an_accepted_routine_is_documented_under_its_own_name(game):
    net, agent = game
    out = net.compile_and_diff(agent, "func_005_8a10", TEXT, "s")
    assert out.startswith("✅ DOCUMENTED: ClearMenuMap"), out
    fn = net.store.one("SELECT * FROM functions WHERE rom_offset=?", 0x14A10)
    assert (fn["status"], fn["name"]) == ("documented", "ClearMenuMap") and ".DEFINE" not in fn["matched_source"]
    assert net.store.one("SELECT address FROM symbols WHERE name='wMenuOpen'")["address"] == 0xC0A2
    assert net.function_diff("s", "ClearMenuMap")["asm"]["ok"]


def test_one_that_isnt_there_yet_says_why(game):
    net, agent = game
    out = net.compile_and_diff(agent, "func_005_8a10", TEXT.replace("out (PortVDPControl), a", "out ($bf), a"), "s")
    assert out.startswith("❌ not yet (22 bytes, 22 the same)") and "5. line 11" in out
