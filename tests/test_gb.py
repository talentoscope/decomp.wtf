"""Game Boy / Game Boy Color support: cartridge detection, header parsing, and the SM83 (LR35902)
disassembler the fork needs because capstone has no Game Boy backend.

The ROMs here are synthetic: a valid Nintendo logo, a correct header checksum and a power-of-two size
are exactly what the detector checks, so a generated ROM exercises the same path a real cartridge does.
"""

from __future__ import annotations

import shutil

import pytest

from decompnet.rom import detect, gb, sm83
from decompnet.rom.n64 import RomError


def make_rom(size: int = 0x8000, title: bytes = b"TESTGAME", cgb: int = 0x00, cart: int = 0x1B,
             size_code: int = 0x00, ram: int = 0x03, code: bytes = b"") -> bytes:
    """A cartridge with a real logo and a valid header checksum. `code` is placed at 0x150."""
    rom = bytearray(size)
    rom[0x100:0x104] = bytes([0x00, 0xC3, 0x50, 0x01])  # nop · jp $0150
    if code:
        rom[0x150:0x150 + len(code)] = code
    rom[0x104:0x134] = gb.LOGO
    rom[0x134:0x134 + len(title)] = title
    rom[0x143] = cgb
    rom[0x147] = cart
    rom[0x148] = size_code
    rom[0x149] = ram
    rom[0x14A] = 0x01
    rom[0x14C] = 0x00
    rom[0x14D] = gb.header_checksum(rom)
    return bytes(rom)


# ------------------------------------------------------------------ detection

def test_detects_a_game_boy_rom_and_its_platform():
    rom = make_rom()
    assert gb.is_gb(rom)
    picked = detect.identify(rom, "test.gb")
    assert picked.platform == "gb"
    assert picked.filename == "rom.gb"
    assert picked.big_endian is False
    assert picked.info["title"] == "TESTGAME"
    assert detect.PLATFORMS["gb"] == "Game Boy"


def test_detects_game_boy_color_and_reports_its_cgb_mode():
    rom = make_rom(title=b"COLORGAME", cgb=0xC0, size_code=0x01)
    picked = detect.identify(rom, "color.gbc")
    assert picked.platform == "gbc" and picked.filename == "rom.gbc"
    assert picked.info["cgb"]["mode"] == "cgb-only"
    enhanced = gb.read_info(make_rom(title=b"ENHANCED", cgb=0x80))
    assert enhanced["platform"] == "gbc" and enhanced["cgb"]["mode"] == "cgb-enhanced"
    assert detect.PLATFORMS["gbc"] == "Game Boy Color"


def test_rejects_near_misses():
    good = make_rom()
    broken_logo = bytearray(good); broken_logo[0x104] ^= 0xFF
    assert not gb.is_gb(bytes(broken_logo))
    broken_sum = bytearray(good); broken_sum[0x14D] ^= 0xFF
    assert not gb.is_gb(bytes(broken_sum))
    assert not gb.is_gb(b"\0" * 0x8000)                      # no logo
    assert not gb.is_gb(good[:0x1000])                       # truncated
    with pytest.raises(RomError):
        gb.read_info(b"\0" * 0x8000)
    # a ROM whose length is not a whole number of banks is not a cartridge dump
    assert not gb.is_gb(good + b"\0" * 0x1234)
    # a Game Boy ROM must not be mistaken for a GBA ROM, and vice versa
    assert not detect._program(good).platform == "gba"
    from decompnet.rom import gba
    assert not gba.is_gba(good)


def test_header_fields_match_the_cartridge():
    rom = make_rom(size=0x20000, title=b"POKEMON", cart=0x13, size_code=0x02, ram=0x03)
    info = gb.read_info(rom)
    assert info["cartridge"] == "MBC3+RAM+battery" and info["mbc"].startswith("MBC3")
    assert info["rom_banks"] == 8 and info["rom_bytes_declared"] == 0x20000
    assert info["size_mismatch"] is False
    assert info["ram_bytes"] == 0x8000
    assert info["region"] == "elsewhere"
    assert info["entrypoint"] == 0x100
    assert info["header_checksum"] == "0x%02x" % rom[0x14D]
    assert info["global_checksum"].startswith("0x")
    assert info["compiler_guess"]
    # a size code that disagrees with the file length is reported rather than silently trusted
    assert gb.read_info(make_rom(size=0x10000, size_code=0x02))["size_mismatch"] is True
    # a CGB title is 15 bytes: the flag byte must not leak into the title
    assert gb.read_info(make_rom(title=b"ABCDEFGHIJKLMNO", cgb=0xC0))["title"] == "ABCDEFGHIJKLMNO"


def test_banks_are_sixteen_kib():
    rom = make_rom(size=0x10000)
    banks = gb.banks(rom)
    assert len(banks) == 4 and all(len(b) == 0x4000 for b in banks)
    assert banks[0] == rom[:0x4000]


# ------------------------------------------------------------------ the SM83 disassembler

@pytest.mark.parametrize("raw, addr, text", [
    ("00", 0x100, "nop"),
    ("3e12", 0x100, "ld a, $12"),
    ("0605", 0x100, "ld b, $05"),
    ("76", 0x100, "halt"),
    ("010002", 0x100, "ld bc, $0200"),
    ("310000", 0x100, "ld sp, $0000"),
    ("22", 0x100, "ld (hl+), a"),
    ("3a", 0x100, "ld a, (hl-)"),
    ("02", 0x100, "ld (bc), a"),
    ("0a", 0x100, "ld a, (bc)"),
    ("34", 0x100, "inc (hl)"),
    ("3d", 0x100, "dec a"),
    ("19", 0x100, "add hl, de"),
    ("07", 0x100, "rlca"),
    ("2f", 0x100, "cpl"),
    ("37", 0x100, "scf"),
    ("1850", 0x100, "jr $0152"),          # 0x100 + 2 + 0x50
    ("18fb", 0x100, "jr $00fd"),          # signed relative backwards
    ("2005", 0x100, "jr nz, $0107"),
    ("2805", 0x100, "jr z, $0107"),
    ("78", 0x100, "ld a, b"),
    ("47", 0x100, "ld b, a"),
    ("80", 0x100, "add a, b"),
    ("a1", 0x100, "and c"),               # ALU takes no explicit `a,` for the reduce-style ops
    ("b8", 0x100, "cp b"),
    ("c9", 0x100, "ret"),
    ("d9", 0x100, "reti"),
    ("c0", 0x100, "ret nz"),
    ("d8", 0x100, "ret c"),
    ("c5", 0x100, "push bc"),
    ("f1", 0x100, "pop af"),
    ("c3cdab", 0x100, "jp $abcd"),
    ("caefbe", 0x100, "jp z, $beef"),
    ("cd3412", 0x100, "call $1234"),
    ("c4efbe", 0x100, "call nz, $beef"),
    ("c7", 0x100, "rst $00"),
    ("ff", 0x100, "rst $38"),
    ("c612", 0x100, "add a, $12"),
    ("d612", 0x100, "sub $12"),
    ("f633", 0x100, "or $33"),
    ("fe05", 0x100, "cp $05"),
    ("e012", 0x100, "ldh ($ff00+$12), a"),
    ("f012", 0x100, "ldh a, ($ff00+$12)"),
    ("e2", 0x100, "ld ($ff00+c), a"),
    ("f2", 0x100, "ld a, ($ff00+c)"),
    ("e8fe", 0x100, "add sp, $fe"),        # signed offset, rendered as its byte
    ("f8fe", 0x100, "ld hl, sp+$fe"),
    ("e9", 0x100, "jp hl"),
    ("f9", 0x100, "ld sp, hl"),
    ("ea3412", 0x100, "ld ($1234), a"),
    ("fa3412", 0x100, "ld a, ($1234)"),
    ("f3", 0x100, "di"),
    ("fb", 0x100, "ei"),
    ("08 3412".replace(" ", ""), 0x100, "ld ($1234), sp"),
])
def test_instruction_spot_checks(raw, addr, text):
    got, ln = sm83.decode(bytes.fromhex(raw), addr)
    assert got == text and ln == len(bytes.fromhex(raw))


@pytest.mark.parametrize("byte, text", [
    (0x07, "rlc a"), (0x00, "rlc b"), (0x06, "rlc (hl)"),
    (0x0F, "rrc a"), (0x17, "rl a"), (0x1F, "rr a"),
    (0x27, "sla a"), (0x2F, "sra a"), (0x37, "swap a"), (0x3F, "srl a"),
    (0x40, "bit 0, b"), (0x7E, "bit 7, (hl)"), (0x47, "bit 0, a"),
    (0x80, "res 0, b"), (0x86, "res 0, (hl)"), (0xBF, "res 7, a"),
    (0xC0, "set 0, b"), (0xFF, "set 7, a"), (0xFE, "set 7, (hl)"),
])
def test_cb_prefixed_spot_checks(byte, text):
    got, ln = sm83.decode(bytes([0xCB, byte]), 0x100)
    assert got == text and ln == 2


def test_undefined_opcodes_decode_as_data():
    for op in sorted(sm83.UNDEFINED):
        text, ln = sm83.decode(bytes([op, 0x11, 0x22]), 0x100)
        assert text == f"db ${op:02x}" and ln == 1


def test_every_base_and_cb_opcode_decodes():
    """The whole map, not a sample: nothing may raise, and every length must be 1-3 bytes."""
    for op in range(0x100):
        text, ln = sm83.decode(bytes([op, 0x34, 0x12]), 0x100)
        assert text and ln in (1, 2, 3), (hex(op), text, ln)
        assert sm83.decode(bytes([op]), 0x100)[1] == 1 if op in (0xCB,) else True
    for op in range(0x100):
        text, ln = sm83.decode(bytes([0xCB, op]), 0x100)
        assert text and ln == 2
    # truncated input never raises: a byte past the end decodes as data
    assert sm83.decode(b"\x3e", 0x100) == ("db $3e", 1)
    assert sm83.decode(b"\xcd\x34", 0x100) == ("db $cd", 1)
    assert sm83.decode(b"\xcb", 0x100) == ("db $cb", 1)
    assert sm83.decode(b"", 0x100) == ("db $00", 1)


def test_disassemble_walks_without_desynchronising():
    # nop · ld a,$12 · call $0200 · ret  (and a data byte in the middle on purpose)
    code = bytes.fromhex("00") + bytes.fromhex("3e12") + bytes.fromhex("cd0002") + b"\xd3" + bytes.fromhex("c9")
    lines = list(sm83.disassemble(code, 0x100))
    assert [addr for addr, _, _ in lines] == [0x100, 0x101, 0x103, 0x106, 0x107]
    assert [t for _, t, _ in lines] == ["nop", "ld a, $12", "call $0200", "db $d3", "ret"]
    assert sum(ln for _, _, ln in lines) == len(code)
    assert sm83.asm_text(code, 0x100, width=6).splitlines()[1] == "000101  ld a, $12"


# ------------------------------------------------------------------ discovery

def test_candidates_include_vectors_entry_and_call_targets():
    code = bytes.fromhex("cd0002") + bytes.fromhex("cd1002") + bytes.fromhex("c9")  # two calls, then ret
    rom = make_rom(code=code)
    found = dict(gb.candidates(rom))
    for vector in gb.VECTORS:
        assert vector in found and found[vector] == "vector"
    assert found[0x100] == "entry"
    assert 0x150 in found          # the code start itself is reached by the entry's `jp`
    assert 0x200 in found and 0x210 in found
    # flat offsets are what discovery returns; the CPU address is derived per bank
    assert gb.cpu_address(0x150) == 0x150 and gb.bank_of(0x150) == 0


def test_bank_and_cpu_addresses_round_trip():
    """Bank 0 is wired to 0x0000-0x3FFF; every other bank appears in the 0x4000-0x7FFF window, so the
    same CPU address can exist in two banks and an address alone is not an identity."""
    assert gb.bank_of(0x178D) == 0 and gb.cpu_address(0x178D) == 0x178D
    assert gb.bank_of(0x432B) == 1 and gb.cpu_address(0x432B) == 0x432B      # bank 1: flat == CPU
    assert gb.bank_of(0x831C) == 2 and gb.cpu_address(0x831C) == 0x431C      # bank 2: mapped to 0x4000+
    assert gb.function_name(0x831C) == "func_002_431c"
    assert gb.function_name(0x000B58) == "func_000_0b58"
    for flat in (0x0000, 0x0150, 0x3FFF, 0x4000, 0x432B, 0x7FFF, 0x8000, 0x831C):
        assert gb.flat_offset(gb.cpu_address(flat), gb.bank_of(flat)) == flat


def test_functions_are_disassembled_with_names_and_refs():
    code = bytes.fromhex("cd0002") + bytes.fromhex("c9")          # at 0x150: call $0200; ret
    rom = make_rom(code=code)
    fns = gb.functions(rom)
    by_addr = {f.vram: f for f in fns}
    assert 0x150 in by_addr and by_addr[0x150].name == "func_000_0150"  # bank 0, CPU address (bank_cpu)
    entry_call = by_addr[0x150]
    assert entry_call.n_insns >= 2
    assert "call $0200" in entry_call.asm
    assert (0, 10, 0x200) in entry_call.refs  # (offset, kind=call, address)
    assert all(f.size > 0 and f.asm for f in fns)
    # the entry point is disassembled too (nop · jp $0150)
    assert by_addr[0x100].asm.startswith("0150  nop") or "nop" in by_addr[0x100].asm


def test_split_reads_a_rom_file(tmp_path):
    rom = make_rom(code=bytes.fromhex("c9"))
    path = tmp_path / "game.gb"
    path.write_bytes(rom)
    fns = gb.split(path, tmp_path, prefer_mgbdis=False)  # the built-in sweep (mgbdis's split: test_gb_map.py)
    assert fns and all(f.name.startswith("func_") for f in fns)
    bad = tmp_path / "notarom.gb"
    bad.write_bytes(b"\0" * 0x8000)
    with pytest.raises(RomError):
        gb.split(bad, tmp_path)


def test_import_creates_a_readable_project(tmp_path):
    """End to end: a .gb file imports as a project with disassembled functions. Claimable when the
    pinned GBDK-2020 toolchain is installed (the matching lane is live); assembly-only when it isn't."""
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=bytes.fromhex("cd0002") + bytes.fromhex("c9")))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", slug)
    cfg = __import__("json").loads(proj["config"])
    from decompnet.rom import gb_build

    assert cfg["platform"] == "gb" and cfg["arch"] == "sm83"
    lane = gb_build.available()
    assert cfg["compiler"]["toolchain"] == gb_build.TOOLCHAIN  # named even when not installed: the overview reads it
    assert cfg["compiler"]["provenance"] == ("explicit" if lane else "unavailable")
    fns = net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
    assert len(fns) > 3
    assert all(f["status"] == ("open" if lane else "asm") for f in fns)
    assert all(f["asm"] and f["n_insns"] > 0 for f in fns)
    entry = net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=256", proj["id"])
    assert entry is not None and "jp " in entry["asm"] and "0150" in entry["asm"]  # (mgbdis's split names it)


# ------------------------------------------------------------------ the page's own check of a dropped file

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("cgb, platform", [(0x00, "gb"), (0x80, "gbc"), (0xC0, "gbc")])
def test_the_page_recognises_a_dropped_game_boy_rom(tmp_path, cgb, platform):
    """The drop zone reads the first bytes itself (nothing is uploaded until confirmed): it must agree with the
    server's three checks, or a Game Boy ROM is turned away as 'not a game'."""
    import json
    import subprocess
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("function identifySega(")  # identifyHead calls it
    fn = js[start:js.index("\n}\n", js.index("function identifyHead(head, file) {")) + 3]
    rom = make_rom(cgb=cgb)
    head = list(rom[:0x200])
    script = (f"function crc16() {{ return -1; }}\n{fn}\nconst head = new Uint8Array({json.dumps(head)});\n"
              f"console.log(JSON.stringify(identifyHead(head, {{ name: 'Game.gb', size: {len(rom)} }})));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    assert info["platform"] == platform and info["order"] == "cartridge" and info["cgb"] == cgb
    broken = list(head)
    broken[0x14D] ^= 0xFF  # a bad header checksum: not a Game Boy ROM
    bad = json.loads(subprocess.run(["node", "-e", script.replace(json.dumps(head), json.dumps(broken))],
                                    capture_output=True, text=True, check=True).stdout)
    assert bad is None or bad["platform"] not in ("gb", "gbc")
