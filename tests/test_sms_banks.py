"""Master System code is found by following it with the banks it maps: a call into slot 2 reaches the bank the code
put there and runs at 0x8000, a function that maps the bank its callers pass in A is a bank switcher, a dispatcher's
table names handlers, and a bank of data isn't swept for calls. A project split by an older splitter is split again
keeping its work."""

from __future__ import annotations

import json

from decompnet.app.engine import RomNetwork
from decompnet.rom import sms

from .test_sms import make_rom

SIZE = 0x20000  # 8 banks: the Sega mapper pages slot 2
CODE = {
    0x0000: bytes.fromhex("f3ed56c30001"),        # di · im 1 · jp $0100
    0x0100: bytes.fromhex("3e05" "32ffff" "cd0081"  # ld a,5 · ld ($ffff),a · call $8100   (bank 5, in slot 2)
                          "3e06" "cd0002" "cd0080"  # ld a,6 · call switch · call $8000   (bank 6, through the switcher)
                          "3a00c0" "214001" "c33001"),  # ld a,($c000) · ld hl,table · jp dispatch
    0x0130: bytes.fromhex("875f1600197e23666fe9"),  # dispatch: add a,a · ld e,a · ld d,0 · add hl,de · ld a,(hl) ·
    #                                                 inc hl · ld h,(hl) · ld l,a · jp (hl)
    0x0140: bytes.fromhex("6001" "7001" "ffff"),   # the table: two handlers, then the end
    0x0160: bytes.fromhex("3e01c9"),
    0x0170: bytes.fromhex("3e02c9"),
    0x0200: bytes.fromhex("32ffffc9"),              # switch: ld ($ffff),a · ret   (maps the bank its caller passes)
    0x0300: bytes.fromhex("cd0084c9"),              # only a sweep finds this one: what slot 2 holds here is unknown
    0x0380: bytes.fromhex("cd0003"),                # (its caller, in code nothing reaches)
    0x8400: bytes.fromhex("3e0ac9"),                # bank 2 at 0x8400: code only the power-on guess would reach
    0x14100: bytes.fromhex("cd0082c9"),             # bank 5 at 0x8100: call $8200 (its own bank, in slot 2)
    0x14200: bytes.fromhex("3e07c9"),
    0x18000: bytes.fromhex("3e08c9"),               # bank 6 at 0x8000
    0xC000: bytes.fromhex("cd5003") + bytes(range(16, 64)),  # bank 3: data, with bytes that read like a call
    0x0350: bytes.fromhex("3e09c9"),
}


def _rom():
    return make_rom(size=SIZE, code=CODE)


def test_calls_reach_the_bank_the_code_mapped_where_it_runs():
    starts, bases = sms.candidates(_rom())
    assert starts[0x14100][0] == 0x8100 and starts[0x14200][0] == 0x8200  # bank 5 runs at 0x8000, calls stay in it
    assert starts[0x18000][0] == 0x8000  # mapped by the switcher, from the bank its caller passed in A
    assert bases[5] == bases[6] == 0x8000
    fns = {f.rom: f for f in sms.functions(_rom())}
    assert fns[0x14100].name == "func_005_8100" and fns[0x14100].vram == 0x8100 and fns[0x14100].bank == 5
    assert [t for _o, _k, t in fns[0x14100].refs] == [0x8200]


def test_a_dispatchers_table_names_its_handlers():
    starts, _ = sms.candidates(_rom())
    assert starts[0x0160][1] == "table" and starts[0x0170][1] == "table"
    assert 0x0130 in starts  # the dispatcher several could jump to: a function of its own


def test_guesses_that_would_land_in_data_are_not_made():
    starts, _ = sms.candidates(_rom())
    assert 0x0300 in starts  # found by the sweep...
    assert 0x8400 not in starts  # ...but slot 2 holds whatever its caller mapped: not the power-on bank 2
    assert 0x0350 not in starts  # bank 3 is data: its CD bytes aren't calls


def test_a_symbol_file_next_to_the_rom_names_and_seeds_functions(tmp_path):
    rom = tmp_path / "game.sms"
    rom.write_bytes(_rom())
    (tmp_path / "game.sym").write_text("; WLA-DX\n[labels]\n0000:0160 HandlerA\n0005:8200 Bank5Helper\n"
                                       "0000:0170 _local\n0007:8800 Unreached\n")
    fns = {f.rom: f for f in sms.split(rom, tmp_path, log=lambda *_: None)}
    assert fns[0x0160].name == "HandlerA" and fns[0x14200].name == "Bank5Helper"
    assert fns[0x0170].name == "func_000_0170"  # local labels are left out


def test_a_project_split_by_an_older_splitter_is_split_again_keeping_its_work(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    slug = net.import_rom(_rom(), confirmed=True, name="game.sms", log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    agent, _ = net.local_agent("claude-1")
    with net.store.tx() as db:  # as an older split left it: a function cut in the middle of main, with a note,
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                   "VALUES(?, 'func_000_0105', 4, 1, ?, '[]', '', 'bank000', 261, 261, 'open')", (pid, b"\x32\xff\xff\xcd"))
        bogus = db.execute("SELECT id FROM functions WHERE name='func_000_0105'").fetchone()[0]
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,0)",
                   (pid, bogus, agent.id, "starts mid-routine"))
        db.execute("UPDATE functions SET status='matched', matched_source='/* a */' WHERE project_id=? AND rom_offset=?",
                   (pid, 0x0160))
        db.execute("UPDATE functions SET name='LoadTwo', vram=28928 WHERE project_id=? AND rom_offset=?", (pid, 0x0170))
    preview = net.resplit_banked(slug, apply=False, log=lambda *_: None)
    assert preview["removed"] == 2 and preview["notes_moved"] == 1 and preview["matched_kept"] == 1
    assert net._function("func_000_0105", slug)  # a preview changes nothing
    out = net.resplit_banked(slug, apply=True, log=lambda *_: None)
    rows = {r["rom_offset"]: r for r in net.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
    assert 261 not in rows and rows[0x0160]["status"] == "matched" and out["kept"] >= 1
    assert rows[0x0170]["name"] == "LoadTwo" and rows[0x0170]["vram"] == 0x0170  # the name stays, the address is right
    note = net.store.one("SELECT function_id FROM notes WHERE text='starts mid-routine'")
    assert note["function_id"] == rows[0x0100]["id"]  # moved to main, which covers where it started
    segments = {s["name"]: s["vram"] for s in json.loads(net.project_row(slug)["config"])["segments"]}
    assert segments["bank005"] == 0x8000 and segments["bank001"] == 0x4000


def test_importing_a_rom_brings_the_symbol_file_next_to_it(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "game.sms").write_bytes(_rom())
    (src / "game.sym").write_text("[labels]\n0000:0160 HandlerA\n")
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    slug = net.import_rom(src / "game.sms", confirmed=True, log=lambda *_: None)
    assert net._function("HandlerA", slug)["rom_offset"] == 0x0160
