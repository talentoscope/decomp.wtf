"""Master System / Game Gear detection, bank-aware disassembly, SDCC matching and WLA-DX rebuilds."""

from __future__ import annotations

import gc
import time
from pathlib import Path

import pytest

from decompnet import scoring
from decompnet.app.engine import RomNetwork
from decompnet.app.tools import GUIDES, PLATFORM_GUIDES
from decompnet.elfobj import extract_function
from decompnet.rom import detect, gb, gb_build, sms, sms_asm, z80
from decompnet.rom.n64 import RomError


def make_rom(size=0x8000, *, region=4, header=True, code=None):
    data = bytearray(size)
    for offset, body in (code or {}).items():
        data[offset:offset + len(body)] = body
    if header:
        at = 0x7ff0 if size >= 0x8000 else 0x3ff0 if size >= 0x4000 else 0x1ff0
        data[at:at + 8] = sms.MAGIC
        data[at + 0xC:at + 0xF] = bytes((0x23, 0x45, 0x10))
        data[at + 0xF] = (region << 4) | {0x2000: 0xA, 0x4000: 0xB, 0x8000: 0xC}.get(size, 0xC)
        data[at + 0xA:at + 0xC] = sms.checksum(bytes(data), at).to_bytes(2, "little")
    return bytes(data)


def test_detection_header_copier_headerless_and_gb_priority():
    sms_rom = make_rom()
    gg_rom = make_rom(region=6)
    assert detect.identify(sms_rom, "unrelated.bin").platform == "sms"
    assert detect.identify(gg_rom, "wrong.sms").platform == "gg"
    assert detect.identify(bytes(0x200) + sms_rom, "copy.gg").data == sms_rom
    assert detect.identify(make_rom(header=False), "Alex Kidd.SMS").platform == "sms"
    with pytest.raises(RomError):
        detect.identify(make_rom(header=False), "file.bin")
    bad = bytearray(sms_rom)
    bad[0x7ffa] ^= 0xFF
    assert sms.read_info(bytes(bad))["checksum_ok"] is False
    assert detect.identify(make_gb_rom(), "also.sms").platform == "gb"
    assert "sms" in GUIDES and PLATFORM_GUIDES["gg"] == ["sms"]


def make_gb_rom():
    data = bytearray(0x8000)
    data[0x104:0x134] = gb.LOGO
    data[0x147] = 0
    data[0x148] = 0
    data[0x14d] = gb.header_checksum(data)
    return bytes(data)


def close_network(net):
    deadline = time.monotonic() + 5
    while net.twins._busy and time.monotonic() < deadline:
        time.sleep(0.05)
    net.store.conn.close()
    del net
    gc.collect()


def test_bank_identity_trace_and_z80_scoring():
    assert [sms.slot_base(bank, 0x40000) for bank in range(4)] == [0, 0x4000, 0x4000, 0x4000]
    assert sms.target_flat(0x4123, 3, 0x40000) == 0xC123
    assert sms.target_flat(0x8123, 0, 0xC000) == 0x8123
    code = {
        0x0000: bytes.fromhex("f3ed56c38000"),
        0x0038: b"\xc9",
        0x0080: bytes.fromhex("cd0041" "18fe"),
        0x0100: b"\xc9",
        0x4000: b"\xc9",
        0x8000: bytes.fromhex("cd0080c9"),
    }
    functions = sms.functions(make_rom(size=0xC000, code=code))
    target = next(f for f in functions if f.rom == 0x8000)
    assert (target.bank, target.vram, target.code) == (2, 0x8000, bytes.fromhex("cd0080c9"))
    insns = z80.disassemble(bytes.fromhex("3e05d3bf"), 0x100)
    assert [row[1] for row in insns] == ["ld a, $05", "out ($bf), a"]
    a = scoring.disassemble(extract_function(
        gb_build.build_payload("void f(void) { }", name="f", arch="z80"), "f"))
    assert a and a[0].mnemonic == "ret"


def test_mapper_write_controls_the_bank_resolved_by_calls():
    image = make_rom(size=0x10000, code={
        0x0000: bytes.fromhex("cd8000c9"),
        0x0080: bytes.fromhex("3e0332ffffcd0080c9"),
        0xC000: bytes.fromhex("3e07c9"),
    })
    found = sms.functions(image)
    target = next(f for f in found if f.rom == 0xC000)
    assert (target.name, target.bank, target.vram, target.code) == (
        "func_003_8000", 3, 0x8000, bytes.fromhex("3e07c9"))


HEADER = "typedef unsigned char u8;\ntypedef unsigned short u16;\n"
SOURCES = {
    "func_000_0100": "u8 func_000_0100(u8 a, u8 b) { u8 r = a + b; if (r < a) return 0xFF; return r; }\n",
    "func_000_0110": "void func_000_0110(void) { }\n",
    "func_000_0120": "u16 func_000_0120(const u8 *p, u8 n) { u16 s = 0; while (n--) s += *p++; return s; }\n",
}


def sdcc_rom():
    code = {0x0000: bytes.fromhex("f3ed56c38000"),
            0x0080: bytes.fromhex("cd0001" "cd1001" "cd2001" "18fe")}
    symbols = {name: int(name[-4:], 16) for name in SOURCES}
    for name, source in SOURCES.items():
        code[symbols[name]] = gb_build.compile_function(
            source, name, symbols=symbols, context_header=HEADER, arch="z80").code
    return make_rom(code=code)


@pytest.mark.skipif(not gb_build.available("z80"), reason="GBDK-2020 Z80 tools are not installed")
def test_sdcc_z80_compiles_and_matches_through_app(tmp_path, monkeypatch):
    monkeypatch.setattr(RomNetwork, "auto_install_gb_tools", False)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    try:
        slug = net.import_rom(sdcc_rom(), name="game.sms", confirmed=True, log=lambda *_: None)
        overview = net.overview(slug)
        assert overview["platform"] == "sms" and overview["compiler"]["toolchain"] == "gbdk-2020"
        assert overview["exportable"] and overview["compilers"] == ["gbdk-2020"]
        rows = {r["name"]: r for r in net.store.all(
            "SELECT name, status FROM functions WHERE project_id=?", net._project(slug)["id"])}
        assert all(rows[name]["status"] == "open" for name in SOURCES)
        agent, _ = net.local_agent("sms-test")
        for name, source in SOURCES.items():
            net.claim(agent, slug, name)
            result = net.compile_and_diff(agent, name, source, slug)
            assert result.startswith("✅ MATCH"), (name, result)
        assert net.overview(slug)["matched"] == len(SOURCES)
        first = net.store.one("SELECT * FROM functions WHERE project_id=? AND name='func_000_0100'",
                              net._project(slug)["id"])
        assert net._target_obj(first, net._project(slug)).arch == "z80"
    finally:
        close_network(net)


def test_no_z80_toolchain_imports_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(RomNetwork, "auto_install_gb_tools", False)
    monkeypatch.setattr(gb_build, "available", lambda arch="sm83": False)

    def split_banks(_rom, _workdir, *, log, report):
        report.update(bank_base={1: 0x4000, 2: 0x4000}, regions=[])
        return [
            gb.GbFunction("func_001_4000", 0x4000, 0x4000, b"\xc9", 1, "ret", bank=1),
            gb.GbFunction("func_002_4000", 0x4000, 0x8000, b"\xd9", 1, "reti", bank=2),
        ]

    monkeypatch.setattr(sms, "split", split_banks)
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    try:
        slug = net.import_rom(make_rom(size=0xC000, code={0x4000: b"\xc9", 0x8000: b"\xd9"}),
                              name="readonly.sms", confirmed=True, log=lambda *_: None)
        assert net.overview(slug)["platform"] == "sms"
        assert net.overview(slug)["compiler"]["toolchain"] == "gbdk-2020"
        proj = net._project(slug)
        rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY rom_offset", proj["id"])
        assert [r["status"] for r in rows] == ["asm", "asm"]
        assert [net.reader(proj, row)(0x4000, 1) for row in rows] == [b"\xc9", b"\xd9"]
    finally:
        close_network(net)


@pytest.mark.skipif(not sms_asm.available(), reason="WLA-DX is not installed")
def test_wla_judges_documented_z80_assembly_byte_exactly():
    routine = sms_asm.Routine("return_zero", 0, 0x100, b"\xc9", {}, {0x100: "return_zero"}, {0x100: 0x100})
    verdict = sms_asm.judge(
        routine,
        "; Returns to the caller without changing state.\n"
        "; The caller owns all inputs and outputs for this routine.\n"
        "return_zero:\n"
        "    ret\n",
    )
    assert verdict.ok and verdict.same == verdict.size == 1, verdict.problems


@pytest.mark.skipif(not sms_asm.available(), reason="WLA-DX is not installed")
def test_sms_export_rebuilds_a_synthetic_rom_byte_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(RomNetwork, "auto_install_gb_tools", False)
    monkeypatch.setattr(gb_build, "available", lambda arch="sm83": False)
    monkeypatch.setattr(sms, "split", lambda *_args, **_kwargs: [])
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    try:
        slug = net.import_rom(make_rom(), name="build.sms", confirmed=True, log=lambda *_: None)
        exported = net.export_rom(slug, log=lambda *_: None)
        assert exported["identical"] is True
        out = Path(exported["path"])
        assert (out / "layout.json").is_file()
        assert (out / "licenses" / "synaptic-sms-MIT.txt").is_file()
        assert (out / "build" / "game.sms").read_bytes() == make_rom()
    finally:
        close_network(net)
