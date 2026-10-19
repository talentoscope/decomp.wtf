"""NES and Master System games are hand-written assembly, and their splits start a function wherever code branches
to: what they hand out that no C is (pieces of routines, data, padding) goes out of agents' way, as on the Game Boy.
No compiler needed: the project is made from its bytes."""

import json
import time

import pytest

from decompnet.app.engine import RomNetwork

H = bytes.fromhex
GAMES = {
    "nes": ("mos6502", 0x8000, [  # name, bytes, status
        ("piece", H("48"), "open"),                       # pha: runs on into the next piece
        ("table", H("a90002"), "open"),                   # no instruction
        ("routine", H("ad2105cd2505f0038d25052061fe60"), "open"),
        ("stub", H("60"), "matched"),
    ]),
    "sms": ("z80", 0x0100, [
        ("piece", H("eda0eda0"), "open"),                 # ldi; ldi
        ("padding", H("ffffffff"), "open"),
        ("routine", H("3a06cad3bfc9"), "open"),
        ("store", H("af32a1c0c9"), "matched"),
    ]),
}


@pytest.mark.parametrize("platform", GAMES)
def test_pieces_and_data_go_out_of_agents_way(tmp_path, platform):
    arch, base, functions = GAMES[platform]
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": platform, "arch": arch, "rom": {"size": 0x20000, "sha1": "a" * 40, "file": "rom"},
           "compiler": {"toolchain": "gbdk-2020", "flags": ["--fsigned-char"]}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "GAME", "", arch, 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "basic types", time.time(), ""))
        at = base
        for name, code, status in functions:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, name, len(code), 1, code, "[]", "", "", at, at, status))
            at += len(code)
    assert net.twins.set_aside_asm(net.project_row("game")) == 2
    fns = {r["name"]: r for r in net.store.all("SELECT * FROM functions")}
    assert "runs on" in json.loads(fns["piece"]["meta"])["asm_code"]
    assert {n: r["status"] for n, r in fns.items()} == {**{n: "asm" for n, _, _ in functions[:2]},
                                                        functions[2][0]: "open", functions[3][0]: "matched"}
