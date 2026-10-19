"""Master System and Game Gear: detection, header, banks, function discovery, the page's own check of a dropped file,
and (with the GBDK-2020 toolchain installed) matching real SDCC output through the whole app."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import detect, gb_build, sms
from decompnet.rom.n64 import RomError

from .test_gb import make_rom as make_gb_rom

needs_toolchain = pytest.mark.skipif(not gb_build.available("z80"),
                                     reason="the Z80 C toolchain (gbdk-2020) is not installed")

SIZE_CODE = {0x2000: 0xA, 0x4000: 0xB, 0x8000: 0xC, 0xC000: 0xD, 0x10000: 0xE, 0x20000: 0xF}
HEADER_AT = {0x2000: 0x1FF0, 0x4000: 0x3FF0}


def make_rom(size: int = 0x8000, region: int = 4, header: bool = True, code: dict[int, bytes] | None = None,
             product: bytes = b"\x23\x45\x10") -> bytes:
    """A cartridge of `size` bytes (zeros are `nop`s) with `code` placed at the given offsets and, unless told not
    to, a `TMR SEGA` header with a correct checksum."""
    rom = bytearray(size)
    for off, blob in (code or {}).items():
        rom[off:off + len(blob)] = blob
    if header:
        at = HEADER_AT.get(size, 0x7FF0)
        rom[at:at + 8] = sms.MAGIC
        rom[at + 0xC:at + 0xF] = product
        rom[at + 0xF] = (region << 4) | SIZE_CODE.get(size, 0xC)
        rom[at + 0xA:at + 0xC] = sms.checksum(bytes(rom), at).to_bytes(2, "little")
    return bytes(rom)


# ------------------------------------------------------------------ detection and the header

@pytest.mark.parametrize("region, platform, label", [(3, "sms", "SMS Japan"), (4, "sms", "SMS Export"),
                                                     (5, "gg", "GG Japan"), (6, "gg", "GG Export"),
                                                     (7, "gg", "GG International")])
def test_the_region_in_the_header_says_which_console(region, platform, label):
    rom = make_rom(region=region)
    assert sms.is_sms(rom) and sms.platform_of(rom) == platform
    info = sms.read_info(rom)
    assert info["platform"] == platform and info["region"] == label and info["header"] is True
    assert info["checksum_ok"] is True and info["rom_banks"] == 2 and info["size"] == 0x8000
    assert info["rom_bytes_declared"] == 0x8000 and info["size_mismatch"] is False
    assert info["game_code"] == "14523"  # the product code is a BCD number: 5 digits spread over the header bytes
    picked = detect.identify(rom, "game.bin")
    assert picked.platform == platform and picked.filename == f"rom.{platform}" and picked.data == rom
    assert detect.PLATFORMS[platform]


def test_a_wrong_checksum_is_reported_but_the_rom_is_still_taken():
    rom = bytearray(make_rom())
    rom[0x7FFA] ^= 0xFF
    assert sms.is_sms(bytes(rom)) and sms.read_info(bytes(rom))["checksum_ok"] is False


@pytest.mark.parametrize("size", [0x2000, 0x4000, 0xC000, 0x10000, 0x20000])
def test_the_header_sits_where_the_size_puts_it(size):
    rom = make_rom(size=size)
    assert sms.header_offset(rom) == HEADER_AT.get(size, 0x7FF0) and sms.is_sms(rom)
    assert sms.read_info(rom)["rom_banks"] == -(-size // sms.BANK_SIZE)


def test_a_header_in_the_wrong_place_for_the_size_is_not_a_header():
    for size, wrong in ((0x4000, 0x1FF0), (0x8000, 0x3FF0), (0x10000, 0x1FF0), (0x2000, 0x0FF0)):
        rom = bytearray(size)
        rom[wrong:wrong + 8] = sms.MAGIC  # where a smaller ROM keeps it
        assert sms.header_offset(bytes(rom)) is None and not sms.is_sms(bytes(rom)), (hex(size), hex(wrong))


def test_a_copier_header_is_dropped():
    rom = make_rom()
    dump = bytes(0x200) + rom
    assert sms.strip_copier_header(dump) == rom and sms.is_sms(dump)
    picked = detect.identify(dump, "x.sms")
    assert picked.data == rom and picked.info["sha1"] == sms.read_info(rom)["sha1"]  # the same game, the same hash


def test_a_rom_without_a_header_is_told_by_its_extension():
    rom = make_rom(header=False, code={0: b"\xc9"})
    assert not sms.is_sms(rom) and not sms.is_sms(rom, "game.bin") and not sms.is_sms(rom, "game.gb")
    assert sms.platform_of(rom, "Game.SMS") == "sms" and sms.platform_of(rom, "x.gg") == "gg"
    info = sms.read_info(rom, "Alex Kidd.sms")
    assert info["header"] is False and info["title"] == "Alex Kidd" and info["game_code"] == "" and info["region"] == "unknown"
    assert detect.identify(rom, "Alex Kidd.sms").platform == "sms"
    with pytest.raises(RomError):
        detect.identify(rom, "mystery.bin")  # nothing says what it is
    assert not sms.is_sms(bytes(0x2001), "odd.sms") and not sms.is_sms(bytes(0x1000), "small.sms")  # sizes that can't be


def test_other_consoles_are_not_taken_for_one():
    gb_rom = make_gb_rom()
    assert not sms.is_sms(gb_rom) and detect.identify(gb_rom).platform == "gb"
    assert detect.identify(gb_rom, "game.sms").platform == "gb"  # a Game Boy ROM is a Game Boy ROM whatever its name


# ------------------------------------------------------------------ banks

def test_banks_sit_where_the_power_on_mapping_puts_them():
    small, big = 0xC000, 0x40000  # three banks, and a ROM that needs the mapper
    assert [sms.slot_base(b, small) for b in range(3)] == [0x0000, 0x4000, 0x8000]
    assert [sms.slot_base(b, big) for b in range(5)] == [0x0000, 0x4000, 0x4000, 0x4000, 0x4000]
    assert sms.cpu_address(0x4123, big) == 0x4123 and sms.cpu_address(0x8123, small) == 0x8123
    assert sms.cpu_address(0x8123, big) == 0x4123  # bank 2 of a big ROM is shown in slot 1
    assert sms.function_name(0x8123, small) == "func_002_8123" and sms.function_name(0x8123, big) == "func_002_4123"
    assert sms.segment_vram(0, big) == 0 and sms.segment_vram(3, big) == 0x4000


def test_a_call_reaches_the_bank_that_is_mapped_where_it_points():
    small, big = 0xC000, 0x40000
    assert sms.target_flat(0x0123, 3, big) == 0x0123          # only the first KiB is fixed
    assert sms.target_flat(0x4123, 0, big) is None           # reset state was not established
    assert sms.target_flat(0x4123, 3, big, 0x4100) == 3 * 0x4000 + 0x123
    assert sms.target_flat(0x8123, 0, small) is None         # size is not mapper selection or reset evidence
    assert sms.target_flat(0x4123, 2, small, 0x8100) is None
    assert sms.target_flat(0xC000, 0, big) is None and sms.target_flat(0xFFFF, 0, big) is None  # RAM and mapper


# ------------------------------------------------------------------ finding functions

DISCOVERY = {
    0x0000: bytes.fromhex("f3ed56c38000"),                   # di · im 1 · jp $0080
    0x0038: bytes.fromhex("c9"),                              # the interrupt handler
    0x0066: bytes.fromhex("ed45"),                            # the NMI
    0x0080: bytes.fromhex("cd0001" "cd1001" "cd5001" "cd2001" "18fe"),  # main: four calls, then loop on itself
    0x0100: bytes.fromhex("b8c83effc9"),                      # cp b · ret z · ld a,$ff · ret    (a ret in the middle)
    0x0110: bytes.fromhex("0608af8010fdc9"),                  # ld b,8 · xor a · add a,b · djnz · ret   (a loop)
    0x0120: bytes.fromhex("c30001"),                          # jp $0100          (a tail call)
    0x0150: bytes.fromhex("b8ca5501c93cc9"),                  # cp b · jp z,$0155 · ret · inc a · ret   (branch past a ret)
    0x0200: bytes.fromhex("cd4001"),                          # a call in code nothing runs: the sweep still sees it
    0x0140: bytes.fromhex("3e05c9"),                          # ... to this one
}


def _starts(fns):
    return {f.vram: f for f in fns}


def test_functions_are_found_from_the_vectors_the_calls_and_the_sweep():
    fns = _starts(sms.functions(make_rom(code=DISCOVERY)))
    assert {0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x66} <= set(fns)  # the vectors
    assert {0x80, 0x100, 0x110, 0x120, 0x150, 0x140} <= set(fns)
    assert 0x200 not in fns  # a call site is not a function: only its target is


def test_a_function_ends_at_an_unconditional_return_or_jump_and_only_there():
    fns = _starts(sms.functions(make_rom(code=DISCOVERY)))
    assert fns[0x100].code == bytes.fromhex("b8c83effc9")        # `ret z` in the middle does not end it
    assert fns[0x110].code == bytes.fromhex("0608af8010fdc9")    # a loop's djnz does not end it
    assert fns[0x120].code == bytes.fromhex("c30001") and fns[0x80].code == bytes.fromhex("cd0001cd1001cd5001cd200118fe")
    assert fns[0x150].code == bytes.fromhex("b8ca5501c93cc9")    # the `ret` before a branch target does not end it
    assert fns[0x140].code == bytes.fromhex("3e05c9")
    assert fns[0x100].n_insns == 4 and fns[0x100].bank == 0 and fns[0x100].rom == 0x100
    assert fns[0x100].name == "func_000_0100" and fns[0x100].asm.splitlines()[0].endswith("cp b")


def test_calls_and_tail_jumps_are_recorded_as_references():
    fns = _starts(sms.functions(make_rom(code=DISCOVERY)))
    assert sorted(t for _, kind, t in fns[0x80].refs if kind == 10) == [0x100, 0x110, 0x120, 0x150]
    assert [t for _, _, t in fns[0x120].refs] == [0x100]   # the tail call
    assert not fns[0x110].refs  # its own loop is not a reference


def test_bytes_that_only_look_like_calls_do_not_become_functions():
    # a call whose target is not a function (it decodes into an undefined opcode before any return)
    rom = make_rom(code={**DISCOVERY, 0x0300: bytes.fromhex("cd0004"), 0x0400: bytes.fromhex("ed00")})
    assert 0x400 not in _starts(sms.functions(rom))


def test_functions_in_another_bank_are_found_from_their_own_calls():
    size = 0x10000
    rom = make_rom(size=size, code={0x0000: bytes.fromhex("cd0070" "c9"),  # bank 0: call $7000 (not mapped: bank 1)
                                    0x4000 + 0x100: bytes.fromhex("cd0043c9"),  # bank 1: call $4300 (its own bank)
                                    0x4000 + 0x300: bytes.fromhex("3e07c9")})
    fns = _starts(sms.functions(rom))
    assert 0x4300 in fns and fns[0x4300].bank == 1 and fns[0x4300].rom == 0x4300 and fns[0x4300].name == "func_001_4300"


# ------------------------------------------------------------------ the page's own check of a dropped file

def _page_identify(rom: bytes, filename: str) -> dict | None:
    js = (Path(__file__).resolve().parent.parent / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("function identifySega(")
    end = js.index("\n}\n", js.index("function identifyHead(head, file) {")) + 3
    script = (f"function crc16() {{ return -1; }}\n{js[start:end]}\nconst head = new Uint8Array({json.dumps(list(rom[:0x9400]))});\n"
              f"console.log(JSON.stringify(identifyHead(head, {{ name: {json.dumps(filename)}, size: {len(rom)} }})));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_recognises_a_dropped_master_system_or_game_gear_rom():
    """The drop zone reads the first bytes itself (nothing is uploaded until confirmed) and must agree with the server."""
    assert _page_identify(make_rom(region=4), "game.sms")["platform"] == "sms"
    assert _page_identify(make_rom(region=6), "game.gg")["platform"] == "gg"
    assert _page_identify(make_rom(region=6), "whatever.bin")["platform"] == "gg"  # by header, not by name
    info = _page_identify(bytes(0x200) + make_rom(), "copier.sms")
    assert info["platform"] == "sms" and info["header"] is True  # a copier header in front is skipped
    small = _page_identify(make_rom(size=0x4000), "tiny.sms")
    assert small["platform"] == "sms" and small["header"] is True
    headerless = _page_identify(make_rom(header=False), "Alex Kidd.sms")
    assert headerless["platform"] == "sms" and headerless["header"] is False
    assert _page_identify(make_rom(header=False), "data.bin") is None  # nothing says what it is
    assert _page_identify(make_gb_rom(), "g.sms")["platform"] == "gb"  # a Game Boy header wins


# ------------------------------------------------------------------ real SDCC output, through the whole app

HEADER = "typedef unsigned char u8;\ntypedef unsigned short u16;\n"
SOURCES = {
    "func_000_0100": "u8 func_000_0100(u8 a, u8 b) { u8 r = a + b; if (r < a) return 0xFF; return r; }\n",
    "func_000_0110": "u8 func_000_0100(u8 a, u8 b);\n__at(0xC0C2) u8 counter;\n"
                     "void func_000_0110(void) { counter = func_000_0100(counter, 3); }\n",
    "func_000_0120": "u16 func_000_0120(const u8 *p, u8 n) { u16 s = 0; while (n--) s += *p++; return s; }\n",
}
SYMBOLS = {"func_000_0100": 0x100, "func_000_0110": 0x110, "func_000_0120": 0x120}


def _compiled_rom(region: int) -> bytes:
    code = {0x0000: bytes.fromhex("f3ed56c38000"), 0x0080: bytes.fromhex("cd1001" "cd2001" "18fe")}
    for name, source in SOURCES.items():
        code[SYMBOLS[name]] = gb_build.compile_function(source, name, symbols=SYMBOLS, context_header=HEADER, arch="z80").code
    return make_rom(region=region, code=code)


@needs_toolchain
@pytest.mark.parametrize("region, platform, filename", [(4, "sms", "game.sms"), (6, "gg", "game.gg")])
def test_functions_compiled_by_sdcc_are_found_and_matched_by_the_app(tmp_path, region, platform, filename):
    rom = _compiled_rom(region)
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(rom, name=filename, confirmed=True, log=lambda *_: None)
    net.sms.configure(slug, "sega", "annotated-original", "The authored fixture specifies Sega cartridge mapping.")
    ov = net.overview(slug)
    assert ov["platform"] == platform and ov["compiler"]["toolchain"] == "gbdk-2020" and ov["compiler"]["flags"] == ["--fsigned-char"]
    rows = {r["name"]: r for r in net.store.all("SELECT name, vram, size, status FROM functions WHERE project_id=1")}
    for name in SOURCES:
        assert name in rows and rows[name]["status"] == "open", sorted(rows)
    agent, _ = net.local_agent("tester")
    for name, source in SOURCES.items():
        net.claim(agent, slug, name)
        out = net.compile_and_diff(agent, name, source, slug)
        assert out.startswith("✅ MATCH"), (name, out[:300])
    assert net.overview(slug)["matched"] == 3
    brief = net.get_function("func_000_0100", slug)
    assert "Master System / Game Gear" in brief and "Game Boy (SDCC" not in brief and "in A and the second in L" in brief


@needs_toolchain
def test_a_projects_compiler_flags_reach_sdcc(tmp_path):
    """The loop is `jr` with SDCC's default and `jp` under --opt-code-speed: only the flags tell them apart."""
    loop = "u16 func_000_0120(const u8 *p, u8 n) { u16 s = 0; while (n--) s += *p++; return s; }\n"
    by_default = gb_build.compile_function(loop, "func_000_0120", context_header=HEADER, arch="z80").code
    fast = gb_build.compile_function(loop, "func_000_0120", context_header=HEADER, arch="z80", opt=["--opt-code-speed"]).code
    assert by_default != fast and len(fast) > len(by_default)
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(_compiled_rom(4), name="game.sms", confirmed=True, log=lambda *_: None)
    net.sms.configure(slug, "sega", "annotated-original", "The authored fixture specifies Sega cartridge mapping.")
    agent, _ = net.local_agent("tester")
    net.claim(agent, slug, "func_000_0120")
    net.set_compiler(slug, "gbdk-2020", ["--opt-code-speed"])
    assert not net.compile_and_diff(agent, "func_000_0120", SOURCES["func_000_0120"], slug).startswith("✅")
    net.set_compiler(slug, "gbdk-2020", ["--fsigned-char"])
    assert net.compile_and_diff(agent, "func_000_0120", SOURCES["func_000_0120"], slug).startswith("✅ MATCH")


def test_without_the_toolchain_the_rom_is_imported_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gb_build, "available", lambda arch="sm83": False)
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(make_rom(code=DISCOVERY), name="game.sms", confirmed=True, log=lambda *_: None)
    statuses = {r["status"] for r in net.store.all("SELECT status FROM functions WHERE project_id=1")}
    ov = net.overview(slug)  # the project still opens, and says what is missing
    assert statuses == {"asm"} and ov["platform"] == "sms"  # shown, not handed out
    assert ov["compiler"]["toolchain"] == "gbdk-2020" and ov["compiler"]["installed"] in (True, False)


# ------------------------------------------------------------------ rst calls with an inline byte (__z88dk_shortcall)

RST_SOURCE = ("typedef unsigned char u8;\nvoid func_000_0028(void) __z88dk_shortcall(0x28, 0);\n"
              "u8 func_000_0100(u8 m) { func_000_0028(); return m; }\n")
RST_BYTES = bytes.fromhex("4fc5ef00c179c9")  # ld c,a ; push bc ; rst $28 ; db $00 ; pop bc ; ld a,c ; ret


def test_sdcc_writes_the_byte_after_an_rst_as_defb_and_sdas_reads_dot_db():
    asm = "\trst\t0x28\n\tdefb\t0x00\n  DEFW 0x1234\n\tdefbad\n\tld a, (defb)\n"
    assert gb_build.sdas_directives(asm) == "\trst\t0x28\n\t.db\t0x00\n  .dw 0x1234\n\tdefbad\n\tld a, (defb)\n"


def test_the_listing_keeps_a_data_byte_inside_a_function_and_does_not_invent_functions_from_data():
    listing = """
    00000000                         10 _f::
    00000000 EF               [11]   11 \trst\t0x28
    00000001 00                      12 \t.db\t0x00
    00000002 C9               [10]   13 \tret
    00000003                         20 _table::
    00000003 01 02 03 04             21 \t.db\t0x01, 0x02, 0x03, 0x04
"""
    funcs = gb_build.parse_listing(listing)
    assert funcs["f"].code == bytes.fromhex("ef00c9") and list(funcs) == ["f"]


@needs_toolchain
@pytest.mark.parametrize("arch", ["z80", "sm83"])
def test_a_shortcall_rst_with_its_byte_builds(arch):
    built = gb_build.compile_function(RST_SOURCE, "func_000_0100", symbols={"func_000_0028": 0x28}, arch=arch)
    assert built.code == RST_BYTES


@needs_toolchain
def test_an_rst_call_matches_through_the_app(tmp_path):
    rom = make_rom(code={0x28: b"\xc9", 0x80: bytes.fromhex("cd0001" "18fe"), 0x100: RST_BYTES})
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(rom, name="game.sms", confirmed=True, log=lambda *_: None)
    net.sms.configure(slug, "sega", "annotated-original", "The authored fixture specifies Sega cartridge mapping.")
    agent, _ = net.local_agent("tester")
    net.claim(agent, slug, "func_000_0100")
    out = net.compile_and_diff(agent, "func_000_0100", RST_SOURCE, slug)
    assert out.startswith("✅ MATCH"), out[:400]
