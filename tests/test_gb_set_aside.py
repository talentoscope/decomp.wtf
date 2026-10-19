"""A Game Boy game written in assembly: what its split hands agents that no C can match (padding, graphics, routines
that run on into the next one) goes out of their way, a routine the split ran on ends where it does, and a brief
shows the whole function. No compiler needed: the project is made from its bytes."""

import json
import time

import pytest

from decompnet.app.engine import RomNetwork

H = bytes.fromhex
WRAPPER = H("f09be09c3e14e09bea0021cdef4ef09ce09bea0021c9")  # bank 0x14, call $4eef, bank back, ret
FILL = H("21ff9b0100043e10320b78b120f8c9")  # a fill loop at $2997, then ret
FUNCTIONS = [  # name, CPU address, bytes, status
    ("Call_000_0198", 0x0198, WRAPPER + H("000000") + H("3e08cd9729"), "open"),  # ran on: padding, then more code
    ("Call_000_0011", 0x0011, H("00"), "open"),  # padding in the vector area
    ("Call_002_447c", 0x447C, H("7f7f4040ff00ff00") * 8, "open"),  # tile graphics
    ("Call_000_2997", 0x2997, FILL, "open"),  # a routine SDCC could write
    ("Call_000_3476", 0x3476, H("3802c9c9"), "open"),  # takes a carry from its caller: hand-written
    ("Call_000_3eec", 0x3EEC, H("cd9729") + WRAPPER, "matched"),
]


@pytest.fixture
def game(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "gbc", "arch": "sm83", "rom": {"size": 0x80000, "sha1": "a" * 40, "file": "rom.gbc"},
           "compiler": {"toolchain": "gbdk-2020", "flags": ["--fsigned-char"]}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "GAME", "", "sm83", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "basic types", time.time(), ""))
        for name, vram, code, status in FUNCTIONS:
            rom = vram if vram < 0x4000 else 2 * 0x4000 + vram - 0x4000
            asm = "    ld hl, $9bff\n    ld bc, $0400" if name == "Call_000_2997" else ""  # mgbdis's text, cut at a label
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status,"
                       "best_score,best_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, name, len(code), 1, code, "[]", asm, f"bank{rom // 0x4000:03d}", vram, rom, status,
                        300, "void x(void) {}"))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, name, vram, len(code), "func"))
    from decompnet.rom import sm83
    for name, vram, code, status in FUNCTIONS:  # the real instruction counts
        with net.store.tx() as db:
            db.execute("UPDATE functions SET n_insns=? WHERE name=?", (sum(1 for _ in sm83.disassemble(code, vram)), name))
    return net, net.project_row("game")


def _fn(net, name):
    return net.store.one("SELECT * FROM functions WHERE name=?", name)


def test_what_no_c_matches_goes_out_of_agents_way(game):
    net, proj = game
    assert net.twins.set_aside_asm(proj) == 3
    why = {r["name"]: json.loads(r["meta"] or "{}").get("asm_code") for r in net.store.all("SELECT * FROM functions")}
    assert why["Call_000_0011"].startswith("padding")
    assert "SDCC never writes" in why["Call_002_447c"]
    assert "carry flag" in why["Call_000_3476"]
    assert _fn(net, "Call_000_2997")["status"] == "open" and why["Call_000_2997"] is None
    assert _fn(net, "Call_000_3eec")["status"] == "matched"  # matched work is never touched
    assert net.twins.set_aside_asm(proj) == 0  # nothing new the second time


def test_a_routine_the_split_ran_on_ends_where_it_does(game):
    net, proj = game
    net.twins.set_aside_asm(proj)
    fn = _fn(net, "Call_000_0198")
    assert fn["status"] == "open" and bytes(fn["target"]) == WRAPPER and fn["size"] == len(WRAPPER)
    assert fn["n_insns"] == 10 and fn["best_score"] is None and fn["best_source"] is None  # it starts over
    assert json.loads(fn["meta"])["trimmed_from"] == len(WRAPPER) + 8
    assert [r[2] for r in json.loads(fn["relocs"])] == [0x4EEF]  # its call, nothing past its end
    assert net.store.one("SELECT size FROM symbols WHERE name='Call_000_0198'")["size"] == len(WRAPPER)
    assert any("end where their routine does" in e["detail"] for e in net.live("game")["events"])


def test_a_brief_shows_the_whole_function(game):
    net, proj = game
    text = net._asm_text(_fn(net, "Call_000_2997"), proj)  # mgbdis's text had 2 of its 9 instructions
    lines = [line for line in text.splitlines() if not line.startswith(";")]
    assert len(lines) == 9 and lines[0].endswith("ld hl, $9bff") and lines[-1].endswith("ret")
    assert "[hl-]" in text and "(" not in text  # RGBDS's brackets
    matched = net._asm_text(_fn(net, "Call_000_3eec"), proj)
    assert "call Call_000_2997" in matched and "ldh a, [$ff9b]" in matched  # known functions by name


def test_the_function_list_says_why(game):
    net, proj = game
    net.twins.set_aside_asm(proj)
    rows = {r["name"]: r for r in net.function_map("game")}
    assert rows["Call_000_0011"]["status"] == "asm" and rows["Call_000_0011"]["why"].startswith("padding")
    assert "why" not in rows["Call_000_2997"]


def test_a_room_member_ends_and_sets_aside_the_same_functions(game):
    """A member's copy of the game is the host's: its functions end where the host's do, from the bytes alone."""
    net, proj = game
    net.links["game"] = object()  # this copy is a member's
    assert net.twins.refresh("game") is None
    for _ in range(50):
        if _fn(net, "Call_000_0011")["status"] == "asm":
            break
        time.sleep(0.05)
    assert _fn(net, "Call_000_0011")["status"] == "asm" and _fn(net, "Call_000_0198")["size"] == len(WRAPPER)
    net.links.clear()


def test_the_overview_counts_what_is_set_aside_apart(game):
    """Progress is measured against what C can match: the page shows what's set aside next to it."""
    net, proj = game
    net.twins.set_aside_asm(proj)
    ov = net.overview("game")
    aside = net.store.all("SELECT size FROM functions WHERE status='asm'")
    assert ov["asm"] == 3 and ov["asm_bytes"] == sum(r["size"] for r in aside)
    assert ov["functions"] - ov["asm"] == 3 and ov["matched"] == 1  # 3 to match in C, 1 of them done
