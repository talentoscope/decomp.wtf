"""Game Boy / Game Boy Color detection, bank-aware import, matching payloads and export routing."""

import json
import gc
import importlib.util
import os
import shutil
from pathlib import Path

from decompnet.app.engine import RomNetwork
from decompnet.elfobj import extract_function
from decompnet.rom import detect, export_gb as gb_export, gb, gb_build, gb_mgbdis
from decompnet.rom.export_gb import _blocks
from decompnet import scoring
from decompnet.app.tools import GUIDES, PLATFORM_GUIDES


def cartridge(size_code=1, cgb=0x80):
    data = bytearray(gb.ROM_SIZES.get(size_code, 0x8000))
    data[0x104:0x134] = gb.LOGO
    data[0x134:0x13c] = b"TESTGAME"
    data[0x143] = cgb
    data[0x147] = 0x01
    data[0x148] = size_code
    data[0x14d] = gb.header_checksum(data)
    return bytes(data)


def test_gb_and_gbc_header_detection():
    gbc = cartridge()
    picked = detect.identify(gbc)
    assert picked.platform == "gbc"
    assert picked.info["title"] == "TESTGAME"
    assert picked.info["rom_banks"] == 4
    assert detect.identify(cartridge(cgb=0)).platform == "gb"
    special = detect.identify(cartridge(size_code=0x52))
    assert special.info["size"] == 0x120000 and special.info["rom_banks"] == 72
    assert detect._program(gbc[:0x8000] + b"\0") is None
    corrupt = bytearray(gbc)
    corrupt[0x14d] ^= 1
    assert detect._program(bytes(corrupt)) is None
    assert "gb" in GUIDES and PLATFORM_GUIDES["gbc"] == ["gb"]


def test_flat_offset_and_cpu_address_are_bank_aware():
    assert [gb.cpu_address(i) for i in (0, 0x3fff, 0x4000, 0x8000)] == [0, 0x3fff, 0x4000, 0x4000]
    assert [gb.flat_offset(0x4000, bank) for bank in (1, 2, 3)] == [0x4000, 0x8000, 0xc000]
    assert gb.function_name(0x8000) == "func_002_4000"
    assert gb.refs_of(bytes.fromhex("cd 00 40 c9"), 0x4000, 2, 0x10000, flat_start=0x8000) == []


def test_conditional_returns_and_jumps_do_not_cut_function_extents():
    from decompnet.rom import sm83
    from decompnet.handwritten import sm83_reason, sm83_routine

    assert not sm83.is_return("ret nz")
    assert not sm83.is_return("jr nz, $1234")
    assert sm83.is_return("jr $1234")
    assert sm83.is_return("jp hl")
    assert sm83_reason(b"\xc9\x00") and sm83_routine(b"\xc9\x00") == 1
    assert sm83_routine(b"\x00\x00") is None


def test_builtin_split_resolves_control_flow_within_its_bank():
    data = bytearray(cartridge())
    data[0x100:0x104] = bytes.fromhex("cd 00 40 c9")
    data[0x4000] = 0xc9
    data[0x8000:0x8004] = bytes.fromhex("cd 00 40 c9")
    workspace = Path(__file__).resolve().parent.parent / f".gb-split-work-{os.getpid()}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    try:
        rom_path = workspace / "game.gb"
        rom_path.write_bytes(data)
        found = gb.split(rom_path, workspace, prefer_mgbdis=False, log=lambda *_: None)
        assert any((f.bank, f.rom, f.vram, f.code) == (2, 0x8000, 0x4000, bytes.fromhex("cd 00 40 c9"))
                   for f in found)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_import_keeps_overlapping_bank_addresses_distinct(monkeypatch):
    rom = bytearray(cartridge(cgb=0))
    rom[0x4000], rom[0x8000] = 0xc9, 0xd9
    monkeypatch.setattr(RomNetwork, "auto_install_gb_tools", False)
    monkeypatch.setattr(gb_build, "available", lambda *_: False)
    functions = [
        gb.GbFunction("func_001_4000", 0x4000, 0x4000, b"\xc9", 1, "ret", bank=1),
        gb.GbFunction("func_002_4000", 0x4000, 0x8000, b"\xd9", 1, "reti", bank=2),
    ]

    def fake_split(_rom, _workdir, *, log, report):
        report.update(splitter="built-in", regions=[(0x6000, 4, "table", "test")])
        return functions

    monkeypatch.setattr(gb, "split", fake_split)
    workspace = Path(__file__).resolve().parent.parent / f".gb-test-work-{os.getpid()}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    try:
        net = RomNetwork(workspace / "app.db", workspace)
        slug = net.import_rom(bytes(rom), confirmed=True, log=lambda *_: None)
        proj = net.project_row(slug)
        cfg = net.cfg(proj)
        rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY rom_offset", proj["id"])

        assert cfg["platform"] == "gb"
        assert [(s["rom_start"], s["rom_end"], s["vram"]) for s in cfg["segments"]] == [
            (0, 0x4000, 0), (0x4000, 0x8000, 0x4000), (0x8000, 0xc000, 0x4000), (0xc000, 0x10000, 0x4000)]
        # The cartridge is 64 KiB: there are four 16 KiB banks.
        assert [r["status"] for r in rows] == ["asm", "asm"]
        assert net.reader(proj, rows[0])(0x4000, 1) == b"\xc9"
        assert net.reader(proj, rows[1])(0x4000, 1) == b"\xd9"
        assert net.overview(slug)["exportable"]
        assert net.store.one("SELECT size FROM data_regions WHERE project_id=? AND rom_offset=0x6000",
                             proj["id"])["size"] == 4
        monkeypatch.setattr(gb_build, "available", lambda *_: True)
        net.set_compiler(slug, "gbdk-2020")
        rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY rom_offset", proj["id"])
        assert [(r["name"], r["vram"], r["rom_offset"], r["unit"], r["status"]) for r in rows] == [
            ("func_001_4000", 0x4000, 0x4000, "bank001", "open"),
            ("func_002_4000", 0x4000, 0x8000, "bank002", "open")]
        candidate = gb_build.PAYLOAD_MAGIC + json.dumps({
            "arch": "sm83",
            "functions": {"func_001_4000": {"code": "c9", "start": 0x4000, "relocs": [], "asm": "ret"}},
        }).encode()
        monkeypatch.setattr(gb_build, "build_payload", lambda _source, **_kwargs: candidate)
        result = net._compile(proj, "", "void func_001_4000(void) {}")
        assert result.ok
        compiled, exact, problems = net._evaluate(rows[0], proj, result.obj)
        assert compiled.code == b"\xc9" and exact and not problems
        assert scoring.diff(compiled, compiled).score == 0
        exported = {"path": str(workspace / "source"), "identical": True}
        called = {}

        def fake_export(network, project, out, log):
            called.update(project=project, out=out)
            return exported

        monkeypatch.setattr(gb_export, "export_gb", fake_export)
        assert net.export_rom(slug, log=lambda *_: None) == exported
        assert called["project"] == slug and called["out"].name == "source"
    finally:
        if "net" in locals():
            net.store.conn.close()
            del net
            gc.collect()
        shutil.rmtree(workspace, ignore_errors=True)


def test_matching_payload_is_read_as_sm83_function():
    payload = gb_build.PAYLOAD_MAGIC + json.dumps({
        "arch": "sm83",
        "functions": {"func_001_4000": {"code": "c9", "start": 0x4000, "relocs": [], "asm": "ret"}},
    }).encode()
    obj = extract_function(payload, "func_001_4000")
    assert obj.arch == "sm83" and obj.code == b"\xc9" and obj.relocs == []
    assert gb_build._equates({"func_002_4000": 0x4000}) == "\t.equ _func_002_4000, 0x4000"


def test_installed_windows_toolchains_resolve_executables(monkeypatch, tmp_path):
    from decompnet import toolchains

    gbdk = tmp_path / "gbdk-2020"
    gbdk_bin = gbdk / "gbdk" / "bin"
    gbdk_bin.mkdir(parents=True)
    for binary in ("sdcc.exe", "sdasgb.exe"):
        (gbdk_bin / binary).touch()

    rgbds = tmp_path / "rgbds"
    rgbds.mkdir()
    for binary in ("rgbasm.exe", "rgblink.exe", "rgbfix.exe"):
        (rgbds / binary).touch()

    mgbdis = tmp_path / "mgbdis"
    mgbdis_python = mgbdis / "mgbdis-3.0"
    mgbdis_python.mkdir(parents=True)
    (mgbdis_python / "mgbdis.py").touch()

    roots = {"gbdk-2020": gbdk, "rgbds": rgbds, "mgbdis": mgbdis}
    monkeypatch.setattr(toolchains, "path", lambda tid: roots[tid])

    assert gb_build.available()
    assert gb_mgbdis.available()
    standalone = tmp_path / "export" / "build.py"
    standalone.parent.mkdir()
    shutil.copyfile(Path(gb_export.__file__).with_name("export_gb_build.py"), standalone)
    (standalone.parent / "build.json").write_text("{}", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gb_export_build_test", standalone)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.tools() == (mgbdis_python / "mgbdis.py", rgbds)
    assert module.rgbds_executable(rgbds, "rgbasm") == rgbds / "rgbasm.exe"


def test_export_blocks_keep_each_bank_separate():
    rows = [
        {"rom_offset": 0x4000, "vram": 0x4000, "size": 4, "target": b"\xc9" * 4},
        {"rom_offset": 0x8000, "vram": 0x4000, "size": 4, "target": b"\xc9" * 4},
    ]
    blocks = _blocks(rows, [{"rom_offset": 0x6000, "size": 8}], 0x10000)
    assert "01:4000 .code:2000" in blocks
    assert "02:4000 .code:4000" in blocks
    assert "01:6000 .data:8" in blocks
