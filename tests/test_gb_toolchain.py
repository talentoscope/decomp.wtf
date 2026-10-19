"""The Game Boy C toolchain lane: SDCC/GBDK-2020 → SM83 bytes → the fork's scoring.

The compile tests are skipped when the toolchain isn't installed (it is a download, per platform —
see toolchains.toml); the registry and decoder tests always run.
"""

from __future__ import annotations

import pytest

from decompnet import scoring
from decompnet import toolchains as tc
from decompnet.elfobj import FunctionObj, extract_function
from decompnet.rom import gb, gb_build, sm83

needs_toolchain = pytest.mark.skipif(not gb_build.available(),
                                     reason="the Game Boy C toolchain (gbdk-2020) is not installed")


# ------------------------------------------------------------------ registry

def test_the_gb_toolchain_is_registered_and_pinned():
    t = tc.get("gbdk-2020")
    assert t.arch == "sm83"
    assert t.platform.startswith("Game Boy")
    assert t.kind == "compiler"
    platforms = {p for part in t.parts for p in part.get("platforms", [])}
    assert {"macos-arm64", "macos-x86_64", "linux-x86_64", "linux-aarch64", "windows-x86_64"} <= platforms
    assert all(part.get("sha256") for part in t.parts), "every download must be pinned"


# ------------------------------------------------------------------ the build lane

@needs_toolchain
def test_compiles_a_leaf_function_to_exact_sm83():
    funcs = gb_build.build_source("void f(void) { }")
    assert funcs["f"].code == bytes.fromhex("c9")  # ret
    zero = gb_build.build_source("typedef unsigned char u8;\nu8 f(void) { return 0; }")
    assert zero["f"].code == bytes.fromhex("afc9")  # xor a, a · ret
    eleven = gb_build.build_source("typedef unsigned char u8;\nu8 f(void) { return 0x11; }")
    assert eleven["f"].code == bytes.fromhex("3e11c9")  # ld a, $11 · ret


@needs_toolchain
def test_symbols_resolve_to_the_roms_own_addresses():
    """Absolute mode: the caller supplies the project's symbol table and the candidate's call carries
    the ROM's real address (which is what byte-identical matching needs)."""
    funcs = gb_build.build_source("void g(void);\nvoid f(void) { g(); }", symbols={"g": 0x4006})
    code = funcs["f"].code
    assert code[1:3] == bytes.fromhex("0640"), f"expected the ROM address 0x4006 in {code.hex()}"
    assert code[0] in (0xC3, 0xCD)  # jp (tail call) or call
    # an unknown symbol is not silently assembled as zero
    with pytest.raises(Exception):
        gb_build.build_source("void g(void);\nvoid f(void) { g(); }")


@needs_toolchain
def test_payload_round_trips_into_a_function_object():
    payload = gb_build.build_payload("typedef unsigned char u8;\nu8 f(void) { return 0x11; }", name="f")
    assert payload.startswith(gb_build.PAYLOAD_MAGIC)
    fn = extract_function(payload, "f")
    assert fn is not None and fn.arch == "sm83" and fn.code == bytes.fromhex("3e11c9")


@needs_toolchain
def test_a_reporting_header_and_includes_are_accepted(tmp_path):
    fn = gb_build.compile_function("void f(void) { }", "f", context_header="typedef unsigned char u8;")
    assert fn.code == b"\xc9"


# ------------------------------------------------------------------ scoring (no toolchain needed)

def test_scoring_reads_sm83_instructions_without_capstone():
    insns = list(sm83.Decoder().disasm(bytes.fromhex("3e11c9cd0640"), 0x100))
    assert [(i.mnemonic, i.op_str) for i in insns] == [("ld", "a, $11"), ("ret", ""), ("call", "$4006")]
    assert all(i.id for i in insns)
    assert insns[2].group(None) is True and insns[0].group(None) is False
    assert insns[2].size == 3 and insns[2].address == 0x103  # after ld a,$11 (2) and ret (1)
    assert insns[2].bytes == bytes.fromhex("cd0640")


def test_scoring_matches_identical_sm83_code_and_reports_a_difference():
    target = FunctionObj("func_000b58", "sm83", False, bytes.fromhex("c9"), [])
    same = FunctionObj("f", "sm83", False, bytes.fromhex("c9"), [])
    assert scoring.diff(target, same).score == 0
    other = FunctionObj("f", "sm83", False, bytes.fromhex("afc9"), [])
    assert scoring.diff(target, other).score > 0


def test_the_decoder_covers_the_instruction_set_scoring_will_see():
    # every opcode the assembler can emit must decode to a non-empty mnemonic, or a diff would
    # silently compare garbage
    for op in range(0x100):
        for op2 in (None, 0x00, 0x37):
            code = bytes([op]) if op2 is None else bytes([0xCB, op]) if op == 0xCB else bytes([op, op2])
            text, ln = sm83.decode(code, 0)
            assert text and ln >= 1


@needs_toolchain
def test_the_whole_app_path_matches_a_real_game_boy_function(tmp_path):
    """Import -> claim -> diff -> MATCH, through the same engine code the app runs, on a synthetic
    cartridge whose function body is exactly `ret` (what C `void f(void) {}` compiles to)."""
    from decompnet.app.engine import RomNetwork
    from tests.test_gb import make_rom

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=b"\xc9"))       # one `ret` at 0x150
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", slug)
    cfg = __import__("json").loads(proj["config"])
    assert cfg["arch"] == "sm83" and cfg["compiler"]["toolchain"] == gb_build.TOOLCHAIN
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND target=? AND status='open'",
                       proj["id"], b"\xc9")
    assert fn is not None, "a one-instruction function must be claimable"
    agent = net.authenticate(net.register_agent("gb-tester"))
    net.claim(agent, function=fn["name"])
    out = net.compile_and_diff(agent, fn["name"], f"void {fn['name']}(void) {{ }}\n")
    assert "MATCH" in out, out
    assert net.store.one("SELECT status FROM functions WHERE id=?", fn["id"])["status"] == "matched"
    assert net.store.one("SELECT matched_agent_id FROM functions WHERE id=?", fn["id"])["matched_agent_id"] == agent.id


@needs_toolchain
def test_the_lane_refuses_a_candidate_that_does_not_define_the_function(tmp_path):
    """A candidate must define the function the app is judging — the failure is reported, not scored."""
    with pytest.raises(Exception):
        gb_build.build_payload("void something_else(void) { }", name="func_000b58")


@needs_toolchain
@pytest.mark.parametrize("code, c_source, what", [
    (bytes.fromhex("ea0021c9"), "typedef unsigned char u8;\nvoid {n}(u8 v) {{ *(volatile u8 *)0x2100 = v; }}\n",
     "a sound-register write: ld ($2100), a"),
    (bytes.fromhex("e0f2c9"), "typedef unsigned char u8;\nvoid {n}(u8 v) {{ *(volatile u8 *)0xFFF2 = v; }}\n",
     "an HRAM write: ldh ($ff00+$f2), a"),
])
def test_real_game_shapes_match_through_the_app(tmp_path, code, c_source, what):
    """Non-trivial Game Boy code, not just stubs: the shapes the lane must reproduce are volatile
    register writes — the idiom the brief teaches — and they must match through the whole app path."""
    from decompnet.app.engine import RomNetwork
    from tests.test_gb import make_rom

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=code))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", slug)
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND target=?", proj["id"], code)
    assert fn is not None, f"expected the {what} function to be discovered"
    agent = net.authenticate(net.register_agent("gb-shape"))
    net.claim(agent, function=fn["name"])
    out = net.compile_and_diff(agent, fn["name"], c_source.format(n=fn["name"]))
    assert "MATCH" in out, f"{what}: {out}"


@needs_toolchain
def test_the_lane_enforces_the_source_policy(tmp_path):
    """Inline assembly is a loophole on a platform where targets can be one instruction: `__asm("nop")`
    reproduces them without any C. The Game Boy lane must apply the same source policy as every other."""
    from decompnet.compiler import SourceRejected

    for bad in ('void f(void) __naked {\n    __asm\n        nop\n    __endasm;\n}',
                'void f(void) { __asm("nop"); }',
                'void f(void) { __attribute__((naked)) ; }'):
        with pytest.raises(SourceRejected):
            gb_build.build_source(bad)
    # and a normal C candidate still compiles
    assert gb_build.build_source("void f(void) { }")["f"].code == b"\xc9"


@needs_toolchain
def test_banked_code_uses_cpu_addresses_not_flat_offsets(tmp_path):
    """A bank >= 2 function has a CPU address different from its flat ROM offset. The candidate's calls
    must carry the **CPU** address (what the ROM's instructions contain), which the old flat-address
    model got wrong by 0x4000 per bank."""
    from tests.test_gb import make_rom

    # 4 banks: bank 2 holds a `ret` callee at CPU 0x4000 (flat 0x8000) and a caller at CPU 0x4010
    rom = bytearray(make_rom(size=0x10000))
    rom[0x8000] = 0xC9                                              # callee: ret
    rom[0x8010:0x8014] = bytes.fromhex("cd0040c9")                  # caller: call $4000 ; ret
    rom[0x8020:0x8024] = bytes.fromhex("cd1040c9")                  # something calls the caller (discovery)
    data = bytes(rom)

    fns = {f.name: f for f in gb.functions(data)}
    callee, caller = fns["func_002_4000"], fns["func_002_4010"]
    assert (callee.rom, callee.vram, callee.bank) == (0x8000, 0x4000, 2)
    assert (caller.rom, caller.vram, caller.bank) == (0x8010, 0x4010, 2)
    assert caller.code == bytes.fromhex("cd0040c9")

    symbols = {f.name: f.vram for f in fns.values()}
    src = (f"typedef unsigned char u8;\nu8 {callee.name}(void);\n"
           f"u8 {caller.name}(void) {{ return {callee.name}(); }}\n")
    cand = gb_build.compile_function(src, caller.name, symbols=symbols)
    assert cand.code[1:3] == bytes.fromhex("0040"), (
        f"expected the CPU address 0x4000 in {cand.code.hex()} (flat 0x8000 would be 0080)")


# ------------------------------------------------------------------ the mgbdis split

def test_mgbdis_symbol_parsing_and_code_classification():
    from decompnet.rom import gb_mgbdis

    syms = gb_mgbdis.parse_sym("; File generated by rgblink\n00:0000 RST_00\n02:4abc Func_002_4abc\n"
                              "02:5000 Data_002_5000\n03:4010 .local\n")
    assert syms == [(0, 0x0000, "RST_00"), (2, 0x4ABC, "Func_002_4abc"),
                    (2, 0x5000, "Data_002_5000"), (3, 0x4010, ".local")]
    assert gb_mgbdis.is_code("Func_002_4abc") and gb_mgbdis.is_code("RST_08")
    assert gb_mgbdis.is_code("VBlankInterrupt") and gb_mgbdis.is_code("Call_000_0bd3")
    # internal branch targets and data are NOT function boundaries: mgbdis emits ~27k of them, and
    # treating them as entries cut a 16-byte function into one-byte pieces
    assert not gb_mgbdis.is_code("Jump_000_0bd3") and not gb_mgbdis.is_code("jr_000_0bc7")
    assert not gb_mgbdis.is_code("Data_002_5000") and not gb_mgbdis.is_code(".local")
    assert not gb_mgbdis.is_code("HeaderTitle") and not gb_mgbdis.is_code("HeaderROMSize")
    blocks = gb_mgbdis.blocks('SECTION "ROM Bank $002", ROMX[$4000], BANK[$2]\n'
                              "Func_002_4000::\n    xor a, a\n    ret\n\nData_002_4003::\n    db $12\n")
    assert [ln.strip() for ln in blocks["Func_002_4000"].splitlines()] == ["xor a, a", "ret"]
    assert blocks["Data_002_4003"].strip() == "db $12"


@pytest.mark.skipif(not __import__("decompnet.rom.gb_mgbdis", fromlist=["x"]).available(),
                    reason="mgbdis/rgbds are not installed")
def test_mgbdis_split_finds_code_and_covers_the_rom(tmp_path):
    """mgbdis's split, end to end. The byte-identical reassembly it earns on a real cartridge (verified
    on Zelda: Link's Awakening DX — 1,048,576 bytes identical) is reported in `meta`; a hand-built fixture
    cannot reproduce it, because a synthetic ROM's padding is not what rgbfix writes, so that part is
    asserted where it is meaningful (the real-ROM evidence) rather than faked here."""
    from decompnet.rom import gb_mgbdis
    from tests.test_gb import make_rom

    rom = bytearray(make_rom(size=0x8000))
    # The entry (`jp $0150`) is a jump target, which mgbdis labels Jump_ and the split does not cut at; the code
    # it calls is what becomes functions.
    rom[0x150:0x154] = bytes.fromhex("cd0002c9")           # call $0200 · ret
    rom[0x200:0x207] = bytes.fromhex("ea0021c9e0f2c9")     # a setter and an HRAM write
    rom[0x210:0x213] = bytes.fromhex("af47c9")
    rom[0x154:0x157] = bytes.fromhex("cd1002")             # call $0210
    path = tmp_path / "game.gb"
    path.write_bytes(bytes(rom))
    fns, meta = gb_mgbdis.split(path, tmp_path)
    assert meta["rebuilt_bytes"] == len(rom), "the whole ROM must be covered by the disassembly"
    assert meta["functions"] == len(fns) > 0
    by_flat = {f.rom: f for f in fns}
    assert 0x200 in by_flat and by_flat[0x200].code[:3] == bytes.fromhex("ea0021")  # the setter's bytes
    assert 0x210 in by_flat and by_flat[0x210].code[:3] == bytes.fromhex("af47c9")
    assert all(f.bank == 0 and f.vram == f.rom for f in fns if f.rom < 0x4000)


# ------------------------------------------------------------------ where a function ends

LISTING = """\
ASxxxx Assembler V02.00 + NoICE + SDCC mods  (GameBoy)                  Page 1
                                     46 	.area _CODE
    00000000                         51 _f::
    00000000 4F               [ 4]   52 	ld	c, a
    00000001 83               [ 4]   53 	add	a,e
    00000002 B9               [ 4]   54 	cp	a, c
    00000003 D0               [20]   55 	ret	NC
    00000004 3E FF            [ 8]   56 	ld	a, #0xff
    00000006 C9               [16]   57 	ret
    00000007                         62 _loop::
    00000007                         63 00102$:
    00000007 AF               [ 4]   64 	xor	a, a
    00000008 20 FD            [12]   65 	jr	NZ, 00102$
    0000000A CD 34 12         [24]   66 	call	_g
    0000000D 18 F8            [12]   67 	jr	00102$
    0000000F 00               [ 4]   68 	nop
                                     66 	.area _CODE
"""


def test_a_conditional_return_or_jump_does_not_end_a_function():
    funcs = gb_build.parse_listing(LISTING)
    assert funcs["f"].code == bytes.fromhex("4f83b9d03effc9")  # the code after `ret nc` is part of it
    assert funcs["f"].asm.splitlines()[-2:] == ["ld\ta, #0xff", "ret"]
    # a loop: `jr nz` falls through, the unconditional `jr` closes it, and the padding after it is left out
    assert funcs["loop"].code == bytes.fromhex("af20fdcd341218f8")


@needs_toolchain
def test_a_function_with_an_early_return_is_compiled_whole():
    fn = gb_build.compile_function("typedef unsigned char u8;\n"
                                   "u8 f(u8 a, u8 b) { u8 r = a + b; if (r < a) return 0xFF; return r; }\n", "f")
    assert fn.code == bytes.fromhex("4f83b9d03effc9")


def test_sdcc_runs_under_the_compile_limits(monkeypatch, tmp_path):
    """Helpers and the permuter together could start any number of SDCC processes on agent-written source: they wait
    for the same slots as the other lanes' compilers now, under the same caps (CPU time up to the timeout)."""
    import threading
    import time

    from decompnet import compiler
    from decompnet.rom import gb_build

    running, peak, seen = [0], [0], []
    lock = threading.Lock()

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **kw):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            seen.append(argv)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return Done()

    monkeypatch.setattr(compiler, "_SLOTS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(gb_build.subprocess, "run", run)
    monkeypatch.setattr(gb_build.shutil, "which", lambda name: "/usr/bin/prlimit" if name == "prlimit" else None)
    threads = [threading.Thread(target=gb_build._run, args=(["sdcc", "-v"], tmp_path, 60)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2 and len(seen) == 6
    assert seen[0][0] == "prlimit" and "--cpu=65" in seen[0] and seen[0][-2:] == ["sdcc", "-v"]
