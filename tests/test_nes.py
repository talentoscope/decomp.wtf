"""NES (.nes): the iNES / NES 2.0 header, the mappers' banking, function discovery on the 6502, the page's own check of a
dropped file, and, with the GBDK-2020 toolchain installed, real SDCC output through the whole app."""

from __future__ import annotations

import shutil

import pytest

from decompnet import scoring
from decompnet.app.engine import RomNetwork
from decompnet.elfobj import FunctionObj
from decompnet.rom import detect, gb_build, nes
from decompnet.rom.n64 import RomError

from .test_gb import make_rom as make_gb_rom
from .test_sms import _page_identify

needs_toolchain = pytest.mark.skipif(not gb_build.available("mos6502"), reason="needs the GBDK-2020 toolchain installed")


def make_rom(prg_units: int = 1, mapper: int = 0, chr_units: int = 0, code: dict[int, bytes] | None = None,
             vectors: tuple[int, int, int] | None = None, trainer: bool = False, nes2: bool = False,
             dirty: bool = False, flags6: int = 0) -> bytes:
    """An iNES file: `code` is placed at PRG offsets, `vectors` are (NMI, reset, IRQ) CPU addresses written at the end of
    the PRG (reset defaults to the start of the last bank as the CPU sees it)."""
    prg = bytearray(prg_units * 0x4000)
    for off, blob in (code or {}).items():
        prg[off:off + len(blob)] = blob
    if vectors is not None:
        for i, v in enumerate(vectors):
            prg[len(prg) - 6 + 2 * i:len(prg) - 4 + 2 * i] = v.to_bytes(2, "little")
    f6 = flags6 | ((mapper & 0xF) << 4) | (4 if trainer else 0)
    f7 = (mapper & 0xF0) | (0x08 if nes2 else 0)
    header = bytearray(16)
    header[:4] = nes.MAGIC
    header[4], header[5], header[6], header[7] = prg_units & 0xFF, chr_units & 0xFF, f6, f7
    if nes2:
        header[8] = (mapper >> 8) & 0x0F
        header[9] = ((prg_units >> 8) & 0x0F) | (((chr_units >> 8) & 0x0F) << 4)
    if dirty:
        header[7] = 0
        header[8:16] = b"DiskDude"
    return bytes(header) + (bytes(512) if trainer else b"") + bytes(prg) + bytes(chr_units * 0x2000)


# ------------------------------------------------------------------ the header

def test_the_header_gives_the_mapper_and_the_sizes():
    info = nes.read_info(make_rom(prg_units=2, mapper=30, chr_units=0), "Game.nes")
    assert info["platform"] == "nes" and info["title"] == "Game" and info["mapper"] == 30
    assert info["prg_size"] == 0x8000 and info["chr_size"] == 0 and info["prg_offset"] == 16
    assert "UNROM 512" in info["cartridge"] and "CHR RAM" in info["cartridge"] and info["rom_banks"] == 2
    assert nes.read_info(make_rom(chr_units=1), "g.nes")["chr_size"] == 0x2000


def test_the_mapper_is_split_across_two_header_bytes():
    assert nes.parse_header(make_rom(mapper=0x1E))["mapper"] == 0x1E
    assert nes.parse_header(make_rom(mapper=0xB4))["mapper"] == 0xB4


def test_nes2_extends_the_mapper_and_the_sizes():
    h = nes.parse_header(make_rom(mapper=0x105, nes2=True))
    assert h["mapper"] == 0x105 and h["nes2"] is True


def test_an_old_header_with_a_tool_name_in_it_does_not_invent_a_mapper():
    """"DiskDude!" in bytes 7-15 made old dumps read as mapper 64+: only the low nibble from byte 6 counts then."""
    assert nes.parse_header(make_rom(mapper=0x42, dirty=True))["mapper"] == 0x02 and make_rom(dirty=True)[7:15] == b"\0DiskDud"


def test_a_trainer_moves_the_program():
    rom = make_rom(trainer=True, code={0: b"\xa9\x01"})
    h = nes.parse_header(rom)
    assert h["prg_off"] == 16 + 512 and rom[h["prg_off"]:h["prg_off"] + 2] == b"\xa9\x01"


def test_a_cut_off_dump_is_read_as_far_as_it_goes():
    rom = make_rom(prg_units=2)[:16 + 0x4000 + 0x2000]
    h = nes.parse_header(rom)
    assert h["truncated"] is True and h["prg_size"] == 0x6000
    with pytest.raises(RomError):
        nes.parse_header(make_rom()[:16 + 0x1000])


def test_things_that_are_not_nes_roms_are_refused():
    assert not nes.is_nes(b"NES\x1a" + bytes(8)) and not nes.is_nes(b"\0" * 40000)
    with pytest.raises(RomError):
        nes.parse_header(b"\0" * 40000)
    bad = bytearray(make_rom())
    bad[4] = 0  # no program
    with pytest.raises(RomError):
        nes.parse_header(bytes(bad))
    exponent = bytearray(make_rom(nes2=True))
    exponent[9] = 0x0F
    with pytest.raises(RomError):
        nes.parse_header(bytes(exponent))


def test_the_region_comes_from_the_header():
    assert nes.read_info(make_rom())["region"] == "NTSC"
    pal = bytearray(make_rom())
    pal[9] = 1
    assert nes.read_info(bytes(pal))["region"] == "PAL"


def test_detect_picks_an_nes_rom_by_its_header():
    picked = detect.identify(make_rom(), "x.bin")
    assert picked.platform == "nes" and picked.filename == "rom.nes"
    assert detect.identify(make_gb_rom(), "g.gb").platform == "gb"


# ------------------------------------------------------------------ banking

def _layout(prg_units, mapper):
    h = nes.parse_header(make_rom(prg_units=prg_units, mapper=mapper))
    return nes.layout_of(h)


def test_nrom_shows_its_program_at_8000_and_mirrors_a_16k_one():
    small, big = _layout(1, 0), _layout(2, 0)
    assert small.cpu(0) == 0xC000 and small.target(0xC123, 0) == 0x123 and small.target(0x8123, 0) == 0x123  # the mirror
    assert big.cpu(0) == 0x8000 and big.cpu(0x4000) == 0xC000 and big.target(0xC010, 0) == 0x4010 and big.target(0x8010, 0) == 0x10


def test_uxrom_fixes_the_last_bank_at_c000():
    lay = _layout(4, 2)
    assert [lay.base(b) for b in range(4)] == [0x8000, 0x8000, 0x8000, 0xC000]
    assert lay.target(0xC005, 1) == 3 * 0x4000 + 5          # the fixed bank, from anywhere
    assert lay.target(0x8005, 1) == 0x4000 + 5              # code in the bank shown at 8000 stays in its bank
    assert lay.target(0x8005, 3) == 5                       # from the fixed bank: the power-on bank, 0
    assert lay.target(0x0800, 1) is None and lay.target(0x6000, 1) is None   # RAM and save RAM


def test_the_first_bank_is_fixed_on_mapper_180():
    lay = _layout(4, 180)
    assert lay.base(0) == 0x8000 and lay.base(2) == 0xC000 and lay.target(0x8100, 2) == 0x100 and lay.target(0xC100, 2) == 2 * 0x4000 + 0x100


def test_mmc3_has_8k_banks_with_the_last_two_fixed():
    lay = _layout(2, 4)   # 32 KiB = four 8 KiB banks
    assert lay.unit == 0x2000 and lay.banks == 4 and [lay.base(b) for b in range(4)] == [0x8000, 0x8000, 0xC000, 0xE000]
    assert lay.target(0xE010, 0) == 3 * 0x2000 + 0x10 and lay.target(0xC010, 0) == 2 * 0x2000 + 0x10
    assert lay.target(0x8010, 1) == 0x2000 + 0x10


def test_axrom_shows_32k_banks_at_8000():
    lay = _layout(4, 7)
    assert lay.unit == 0x8000 and lay.banks == 2 and lay.cpu(0x8004) == 0x8004 and lay.target(0xA000, 1) == 0x8000 + 0x2000


def test_an_unknown_mapper_is_read_like_uxrom_and_the_import_says_so():
    lay = _layout(4, 77)
    assert lay.kind == "last" and "read as UxROM" in nes.read_info(make_rom(prg_units=4, mapper=77))["cartridge"]


def test_the_segments_follow_the_banks():
    info = nes.read_info(make_rom(prg_units=4, mapper=2), "g.nes")
    segs = nes.segments(info)
    assert [(s["name"], s["rom_start"], s["rom_end"], s["vram"]) for s in segs] == [
        ("bank000", 16, 16 + 0x4000, 0x8000), ("bank001", 16 + 0x4000, 16 + 0x8000, 0x8000),
        ("bank002", 16 + 0x8000, 16 + 0xC000, 0x8000), ("bank003", 16 + 0xC000, 16 + 0x10000, 0xC000)]


# ------------------------------------------------------------------ function discovery

# NROM-128: the program is at 0xC000 as the CPU sees it (offset 0 here)
RESET = bytes.fromhex("2010c0" "2020c0" "4c06c0")          # jsr $c010 ; jsr $c020 ; jmp $c006 (the main loop: this jmp)
BRANCHY = bytes.fromhex("a905" "c905" "f003" "a900" "60" "a901" "60")   # lda #5; cmp #5; beq +3; lda #0; rts; lda #1; rts
TAIL = bytes.fromhex("4c30c0")                              # jmp $c030
CODE = {0x00: RESET, 0x10: BRANCHY, 0x20: TAIL, 0x30: b"\x60", 0x40: b"\x40", 0x50: b"\x40"}
VECTORS = (0xC040, 0xC000, 0xC050)


def _by_cpu(rom: bytes):
    return {f.vram: f for f in nes.functions(rom)}


def test_functions_are_found_from_the_vectors_the_calls_and_the_tail_jumps():
    fns = _by_cpu(make_rom(code=CODE, vectors=VECTORS))
    assert {0xC000, 0xC010, 0xC020, 0xC030, 0xC040, 0xC050} <= set(fns)
    assert fns[0xC000].name == "func_000_c000"


def test_a_function_ends_at_the_return_after_the_last_branch_and_not_before():
    f = _by_cpu(make_rom(code=CODE, vectors=VECTORS))[0xC010]
    # the first rts is branched over, so the function runs to the second and stops there
    assert f.code == BRANCHY and f.n_insns == 7 and f.asm.splitlines()[2] == "c014  beq $c019"


def test_the_main_loop_jump_to_itself_ends_the_reset_handler_and_is_not_a_new_function():
    fns = _by_cpu(make_rom(code=CODE, vectors=VECTORS))
    assert fns[0xC000].code == RESET and 0xC006 not in fns


def test_calls_and_tail_jumps_are_recorded_as_references():
    fns = _by_cpu(make_rom(code=CODE, vectors=VECTORS))
    assert fns[0xC000].refs == [(0, 10, 0xC010), (3, 10, 0xC020)]
    assert fns[0xC020].refs == [(0, 10, 0xC030)]


def test_a_call_from_code_nothing_reaches_still_names_a_function():
    code = dict(CODE)
    code[0x60] = bytes.fromhex("a200" "e8" "e010" "d0fb" "60")         # ldx #0; inx; cpx #16; bne -5; rts
    code[0x80] = bytes.fromhex("2060c0" "60")                          # jsr $c060 ; rts: nobody calls this
    fns = _by_cpu(make_rom(code=code, vectors=VECTORS))
    assert 0xC060 in fns and 0xC080 not in fns and fns[0xC060].n_insns == 5


def test_a_single_call_to_a_few_bytes_is_not_enough_but_two_calls_are():
    code = dict(CODE)
    code[0x90] = b"\x60"
    code[0xA0] = bytes.fromhex("2090c0" "60")
    assert 0xC090 not in _by_cpu(make_rom(code=code, vectors=VECTORS))   # one caller, one instruction: probably data
    code[0xB0] = bytes.fromhex("2090c0" "60")
    assert 0xC090 in _by_cpu(make_rom(code=code, vectors=VECTORS))


def test_bytes_that_only_look_like_calls_do_not_become_functions():
    code = dict(CODE)
    code[0x70] = bytes.fromhex("20ffff" "2000c0" "2002c0")    # a call to the last byte of the vectors, one to a brk
    assert set(_by_cpu(make_rom(code=code, vectors=VECTORS))) == set(_by_cpu(make_rom(code=CODE, vectors=VECTORS)))


def test_a_table_of_handler_addresses_names_its_handlers():
    handlers = {0x100: bytes.fromhex("a901" "60"), 0x104: bytes.fromhex("a902" "60"), 0x108: bytes.fromhex("a903" "60")}
    direct = bytes.fromhex("00c1" "04c1" "08c1")                        # the addresses themselves
    code = {**CODE, **handlers, 0x200: direct}
    fns = _by_cpu(make_rom(code=code, vectors=VECTORS))
    assert {0xC100, 0xC104, 0xC108} <= set(fns)


def test_a_table_for_rts_dispatch_holds_the_address_minus_one():
    handlers = {0x100: bytes.fromhex("a901" "60"), 0x104: bytes.fromhex("a902" "60"), 0x108: bytes.fromhex("a903" "60")}
    minus_one = bytes.fromhex("ffc0" "03c1" "07c1")                      # $c0ff $c103 $c107: each one below a handler
    fns = _by_cpu(make_rom(code={**CODE, **handlers, 0x200: minus_one}, vectors=VECTORS))
    assert {0xC100, 0xC104, 0xC108} <= set(fns)


def test_two_addresses_in_a_row_are_not_a_table():
    handlers = {0x100: bytes.fromhex("a901" "60"), 0x104: bytes.fromhex("a902" "60")}
    fns = _by_cpu(make_rom(code={**CODE, **handlers, 0x200: bytes.fromhex("00c1" "04c1")}, vectors=VECTORS))
    assert 0xC100 not in fns and 0xC104 not in fns


def test_a_rom_with_nothing_but_filler_has_only_what_the_vectors_name():
    rom = make_rom(code={0: b"\xff" * 0x4000}, vectors=(0xC000, 0xC000, 0xC000))
    assert [f.vram for f in nes.functions(rom)] in ([0xC000], [])  # undefined opcodes make no function worth having


def test_a_rom_with_no_code_the_vectors_or_a_call_can_reach_is_refused():
    with pytest.raises(RomError, match="no code found"):
        nes.functions(make_rom(vectors=(0x1000, 0x2000, 0x3000)))
    assert nes.functions(make_rom(code=CODE, vectors=(0x1000, 0x2000, 0x3000)))   # calls alone still name functions


def test_functions_in_banks_are_found_through_the_bank_their_call_reaches():
    # UxROM, 4 banks: the fixed bank (3) calls into bank 0, and code in bank 1 calls inside its own bank
    fixed = 3 * 0x4000
    code = {fixed + 0x00: bytes.fromhex("2000" "80" "60"),                     # jsr $8000 ; rts
            fixed + 0x10: b"\x40", fixed + 0x20: b"\x40",
            0x0000: bytes.fromhex("a901" "a902" "60"),                          # bank 0 at $8000
            0x4000: bytes.fromhex("2010" "80" "60"),                            # bank 1 calls $8010 (its own) - nobody reaches this
            0x4010: bytes.fromhex("a903" "a904" "60")}
    fns = nes.functions(make_rom(prg_units=4, mapper=2, code=code, vectors=(0xC010, 0xC000, 0xC020)))
    names = {f.name for f in fns}
    assert "func_000_8000" in names and "func_001_8010" in names and "func_003_c000" in names
    assert next(f for f in fns if f.name == "func_001_8010").rom == 16 + 0x4010   # file offsets, header included


def test_a_function_never_crosses_the_end_of_its_bank():
    # UxROM, 2 banks: the fixed bank jumps to the end of bank 0, whose code runs on with no return
    code = {0x3FF0: bytes.fromhex("a900") * 8, 0x4000: bytes.fromhex("4cf0bf")}
    fns = nes.functions(make_rom(prg_units=2, mapper=2, code=code, vectors=(0xC000, 0xC000, 0xC000)))
    runaway = next(f for f in fns if f.name == "func_000_bff0")
    assert runaway.rom + len(runaway.code) <= 16 + 0x4000


# ------------------------------------------------------------------ the engine

@pytest.fixture
def toolchain(monkeypatch):
    state = {"installed": False}
    monkeypatch.setattr(gb_build, "available", lambda arch="sm83": state["installed"])
    return state


def test_an_nes_rom_imports_as_a_banked_project(tmp_path, toolchain):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    rom = make_rom(prg_units=2, mapper=30, code={0x4000 + o: b for o, b in CODE.items()}, vectors=VECTORS)  # in the fixed bank
    slug = net.import_rom(rom, name="Homebrew.nes", confirmed=True, log=lambda *_: None)
    ov = net.overview(slug)
    cfg = net.cfg(net.project_row(slug))
    assert ov["platform"] == "nes" and cfg["arch"] == "mos6502" and ov["compiler"]["toolchain"] == "gbdk-2020"
    assert [(s["name"], s["vram"]) for s in cfg["segments"]] == [("bank000", 0x8000), ("bank001", 0xC000)]
    statuses = {r["status"] for r in net.store.all("SELECT status FROM functions WHERE project_id=1")}
    assert statuses == {"asm"}  # no toolchain: shown, not handed out
    assert ov["functions"] == 6


def test_with_the_toolchain_the_functions_are_handed_out(tmp_path, toolchain):
    toolchain["installed"] = True
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(make_rom(code=CODE, vectors=VECTORS), name="g.nes", confirmed=True, log=lambda *_: None)
    assert {r["status"] for r in net.store.all("SELECT status FROM functions WHERE project_id=1")} == {"open"}
    assert "NES (SDCC 6502" in net.get_function("func_000_c010", slug)


def test_importing_again_once_the_toolchain_is_installed_opens_the_functions(tmp_path, toolchain):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    rom = make_rom(code=CODE, vectors=VECTORS)
    slug = net.import_rom(rom, name="g.nes", confirmed=True, log=lambda *_: None)
    toolchain["installed"] = True
    log: list[str] = []
    again = net.import_rom(rom, name="g.nes", confirmed=True, log=log.append)
    assert again == slug and any("before the C toolchain was installed" in line for line in log)
    assert {r["status"] for r in net.store.all("SELECT status FROM functions WHERE project_id=1")} == {"open"}


def test_the_scoring_reads_6502_code_and_scores_an_operand_difference():
    same = FunctionObj("f", "mos6502", False, bytes.fromhex("a90585088d0020" "60"), [])
    other = FunctionObj("f", "mos6502", False, bytes.fromhex("a90685088d0020" "60"), [])
    assert [i.text for i in scoring.disassemble(same)] == ["lda #$05", "sta $08", "sta $2000", "rts"]
    assert scoring.diff(same, same).score == 0 and scoring.diff(same, same).bytes_match
    changed = scoring.diff(same, other)
    assert changed.score > 0 and not changed.bytes_match and any(marker == "i" for marker, _t, _c in changed.rows)


# ------------------------------------------------------------------ the page's own check of a dropped file

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_recognises_a_dropped_nes_rom():
    """The drop zone reads the first bytes itself (nothing is uploaded until confirmed) and must agree with the server."""
    info = _page_identify(make_rom(prg_units=2, mapper=30), "game.nes")
    assert info["platform"] == "nes" and info["mapper"] == 30 and info["prg"] == 32
    assert _page_identify(make_rom(prg_units=2, mapper=30), "whatever.bin")["platform"] == "nes"   # by header, not by name
    assert _page_identify(make_rom(mapper=0x105, nes2=True), "n2.nes")["mapper"] == 0x105
    assert _page_identify(make_rom(mapper=0x42, dirty=True), "old.nes")["mapper"] == 2     # the junk byte is ignored
    assert _page_identify(b"\0" * 40000, "data.nes") is None   # no header: nothing says what it is


# ------------------------------------------------------------------ the 6502 listing and pins (no toolchain needed)

# an `sdas6500 -l` listing: an instruction that uses a symbol the assembler has no value for is flagged `u`, a branch is
# conditional (code follows it), and a local label (`00104$`) stays inside its function
LISTING = """
ASxxxx Assembler V02.00 + NoICE + SDCC mods  (Rockwell 6502/6510/65C02) Page 2
Hexadecimal [32-Bits]

    00000000                         75 _add:
    00000000 18               [ 2]   83 \tclc
u   00000001 86 00            [ 3]   84 \tstx\t*(REGTEMP+0)
u   00000003 65 00            [ 3]   85 \tadc\t*(REGTEMP+0)
    00000005 D0 02            [ 4]   86 \tbne\t00104$
    00000007 A9 01            [ 2]   87 \tlda\t#0x01
    00000009                         88 00104$:
    00000009 60               [ 6]   92 \trts
    0000000A                        109 _next:
    0000000A 4C 00 00         [ 3]  110 \tjmp\t_add
    0000000D 00 00 00 00      [ 2]  111 \t.ds 4
"""


def test_the_listing_of_the_6502_assembler_is_read_flags_branches_and_all():
    funcs = gb_build.parse_listing(LISTING, "mos6502")
    assert funcs["add"].code == bytes.fromhex("18" "8600" "6500" "d002" "a901" "60")   # the flagged lines are kept; a branch doesn't end it
    assert funcs["next"].code == bytes.fromhex("4c0000")                                # an unconditional jmp does


def test_a_pin_names_a_variable_with_or_without_the_numbers_the_compiler_appends():
    asm = "\t.area _BSS\n_f_PARM_3:\n\t.ds 1\n_f_v_10000_6:\n\t.ds 2\n_f_sloc0_1_0:\n\t.ds 1\n_other:\n\t.ds 1\n"
    out, pinned, left = gb_build._pin_data(asm, {"f_PARM_3": 0x375, "f_v": 0x378, "f_sloc0": 0})
    assert pinned == {"_f_PARM_3": 0x375, "_f_v_10000_6": 0x378, "_f_sloc0_1_0": 0} and left == ["other"]
    assert "_f_PARM_3" not in out and "_other:" in out    # the definition is dropped, so the .equ is the only one
    assert gb_build.pins_in("/* pin a_PARM_2 = 0x0375 */ int x; /*pin b=10*/") == {"a_PARM_2": 0x375, "b": 10}


# ------------------------------------------------------------------ real SDCC output (needs the toolchain)

# What GBDK-2020 4.5.0 built from these functions in a real NES ROM (mapper 30), at 0xC55D, 0xC5A3 and 0xC5C4. The `{n}` is the
# function's name: SDCC names its static variables after it, so a pin has to follow a rename.
CLAMP = "unsigned char {n}(unsigned char x) { if (x > 200) return 200; if (x < 10) return 10; return x; }\n"
CLAMP_BYTES = bytes.fromhex("c9c8f0059003a9c860c90ab002a90a60")
ADD3 = "/* pin {n}_PARM_3 = 0x0375 */\nunsigned char {n}(unsigned char a, unsigned char b, unsigned char c) { return a + b + c; }\n"
ADD3_BYTES = bytes.fromhex("1886346534186d750360")
FILL = ("/* pin {n}_v = 0x0378 */\n/* pin {n}_sloc0 = 0x0000 */\n__at(0x0365) unsigned char table[16];\n"
        "void {n}(unsigned char v) { unsigned char i; for (i = 0; i < 16; i++) table[i] = v + i; }\n")
FILL_BYTES = bytes.fromhex("8d7803a000a200984898186d78038500688434a8a500996503a434c8c01090e560")


@needs_toolchain
@pytest.mark.parametrize("name, source, expected", [("clamp", CLAMP, CLAMP_BYTES), ("add3", ADD3, ADD3_BYTES),
                                                    ("fill", FILL, FILL_BYTES)])
def test_sdcc_builds_the_bytes_a_real_gbdk_rom_has(name, source, expected):
    assert gb_build.compile_function(source.replace("{n}", name), name, arch="mos6502").code == expected


@needs_toolchain
def test_without_a_pin_the_build_says_which_variables_it_left_at_zero():
    notes: list[str] = []
    add3 = ADD3.replace("{n}", "add3")
    built = gb_build.build_source(add3.replace("/* pin add3_PARM_3 = 0x0375 */\n", ""), arch="mos6502", notes=notes)
    assert built["add3"].code == bytes.fromhex("1886346534186d000060") and "add3_PARM_3" in notes[0] and "pin" in notes[0]
    assert gb_build.build_source(add3, arch="mos6502", notes=(pinned := []))["add3"].code == ADD3_BYTES and pinned == []


@needs_toolchain
def test_the_compilers_own_registers_resolve_to_where_gbdk_puts_them():
    code = gb_build.compile_function("unsigned char add(unsigned char a, unsigned char b) { return a + b; }\n", "add",
                                     arch="mos6502").code
    assert code == bytes.fromhex("188634653460")  # clc; stx $34; adc $34; rts: REGTEMP is $0034


@needs_toolchain
def test_a_name_the_project_knows_resolves_to_its_address():
    src = "void func_000_0100(void);\nvoid caller(void) { func_000_0100(); }\n"
    code = gb_build.compile_function(src, "caller", symbols={"func_000_0100": 0xC123}, arch="mos6502").code
    assert code == bytes.fromhex("4c23c1")    # jmp $c123: a call in tail position is a jump
    with pytest.raises(RomError, match="symbols this build does not know"):
        gb_build.compile_function(src, "caller", arch="mos6502")


@needs_toolchain
def test_functions_compiled_by_sdcc_are_found_and_matched_by_the_app(tmp_path):
    placed = {"clamp": 0x10, "add3": 0x40, "fill": 0x60}
    sources = {"clamp": CLAMP, "add3": ADD3, "fill": FILL}
    code = {0x00: bytes.fromhex("2010c0" "2040c0" "2060c0" "4c09c0"), 0x1F0: b"\x40"}
    for name, off in placed.items():
        code[off] = gb_build.compile_function(sources[name].replace("{n}", name), name, arch="mos6502").code
    rom = make_rom(prg_units=2, mapper=30, code={0x4000 + o: b for o, b in code.items()}, vectors=(0xC1F0, 0xC000, 0xC1F0))
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(rom, name="game.nes", confirmed=True, log=lambda *_: None)
    ov = net.overview(slug)
    assert ov["platform"] == "nes" and ov["compiler"]["flags"] == ["--fsigned-char"]
    rows = {r["name"]: r for r in net.store.all("SELECT name, status FROM functions WHERE project_id=1")}
    agent, _ = net.local_agent("tester")
    for name, off in placed.items():
        fn = f"func_001_{0xC000 + off:04x}"
        assert rows[fn]["status"] == "open", sorted(rows)
        net.claim(agent, slug, fn)
        out = net.compile_and_diff(agent, fn, sources[name].replace("{n}", fn), slug)
        assert out.startswith("✅ MATCH"), (name, out[:400])
    assert net.overview(slug)["matched"] == 3


@needs_toolchain
def test_the_app_tells_an_agent_what_is_still_at_address_zero(tmp_path):
    rom = make_rom(prg_units=2, mapper=30, vectors=(0xC1F0, 0xC000, 0xC1F0),
                   code={0x4000: bytes.fromhex("2040c0" "4c03c0"), 0x4040: ADD3_BYTES, 0x41F0: b"\x40"})
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(rom, name="game.nes", confirmed=True, log=lambda *_: None)
    agent, _ = net.local_agent("tester")
    net.claim(agent, slug, "func_001_c040")
    unpinned = ADD3.replace("{n}", "func_001_c040").split("\n", 1)[1]   # without its pin comment
    out = net.compile_and_diff(agent, "func_001_c040", unpinned, slug)
    assert not out.startswith("✅") and "func_001_c040_PARM_3" in out and "/* pin" in out


# ------------------------------------------------------------------ the mappers' layouts

def _mapper(mapper: int, prg_units: int) -> nes.Layout:
    return nes.layout_of({"mapper": mapper, "prg_size": prg_units * nes.PRG_UNIT})


def test_8kib_mappers_show_their_fixed_banks_where_they_are():
    vrc4 = _mapper(21, 8)  # 128 KiB: 16 banks of 8 KiB, the last 16 KiB fixed
    assert (vrc4.unit, vrc4.base(3), vrc4.base(14), vrc4.base(15)) == (0x2000, 0x8000, 0xC000, 0xE000)
    assert vrc4.target(0xC123, 3) == 14 * 0x2000 + 0x123 and vrc4.target(0x8123, 3) == 3 * 0x2000 + 0x123
    assert vrc4.target(0xA123, 3) is None and vrc4.target(0x8123, 15) is None  # (switched: the game's choice)
    mmc2 = _mapper(9, 8)
    assert [mmc2.base(b) for b in (12, 13, 14, 15)] == [0x8000, 0xA000, 0xC000, 0xE000]
    n163 = _mapper(19, 8)
    assert [n163.base(b) for b in (13, 14, 15)] == [0x8000, 0x8000, 0xE000]


def test_vrc6_mapper_31_suro_and_97():
    vrc6 = _mapper(24, 8)  # a 16 KiB bank at $8000 in 8 KiB halves
    assert (vrc6.base(4), vrc6.base(5), vrc6.base(15)) == (0x8000, 0xA000, 0xE000)
    m31 = _mapper(31, 8)  # 32 banks of 4 KiB
    assert (m31.unit, m31.base(9), m31.base(31), m31.target(0xF010, 9)) == (0x1000, 0x9000, 0xF000, 31 * 0x1000 + 0x10)
    surom = _mapper(1, 32)  # 512 KiB: two halves, each with its last bank at $C000
    assert (surom.base(15), surom.base(16), surom.base(31)) == (0xC000, 0x8000, 0xC000)
    assert surom.target(0xC010, 20) == 31 * 0x4000 + 0x10 and surom.target(0xC010, 3) == 15 * 0x4000 + 0x10
    m97 = _mapper(97, 8)
    assert (m97.base(7), m97.base(2), m97.target(0x8010, 2)) == (0x8000, 0xC000, 7 * 0x4000 + 0x10)
