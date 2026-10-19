"""Coverage from an emulator on the Master System and the NES (as on the Game Boy): a Mesen2 code/data log (or an
FCEUX one, on the NES) or an execution trace, imported for the project, starts functions where calls entered and
where code ran that the split didn't reach, and keeps what was only read as data out of functions."""

from __future__ import annotations

import zlib

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.rom import coverage

from .test_coverage import cdl
from .test_nes import make_rom as nes_rom
from .test_sms import make_rom as sms_rom
from .test_sms_corrections import CODE


def _project(tmp_path, name: str, data: bytes):
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    net.auto_install_tools = False
    rom = tmp_path / name
    rom.write_bytes(data)
    return net, net.import_rom(rom, confirmed=True, log=lambda *_: None)


def _starts(net, slug) -> set[int]:
    pid = net._project(slug)["id"]
    return {r["rom_offset"] for r in net.store.all("SELECT rom_offset FROM functions WHERE project_id=?", pid)}


def _regions(net, slug) -> set[tuple]:
    pid = net._project(slug)["id"]
    return {(r["rom_offset"], r["size"], r["kind"]) for r in net.store.all(
        "SELECT * FROM data_regions WHERE project_id=?", pid)}


def test_a_master_system_log_starts_functions_and_keeps_data_out(tmp_path):
    data = sms_rom(size=0x20000, code=CODE)
    net, slug = _project(tmp_path, "game.sms", data)
    assert 0x0700 not in _starts(net, slug) and 0x0900 in _starts(net, slug)
    log = cdl(data, {0x0700: 0x09, 0x0701: 0x01, 0x0702: 0x01, 0x0800: 0x02, 0x0801: 0x02, 0x0802: 0x02})
    info = net.import_coverage(slug, log, "game.cdl")
    assert info["entries"] == 1 and info["from"] == "game.cdl"
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    starts = _starts(net, slug)
    assert 0x0700 in starts and 0x0800 not in starts and 0x0900 not in starts  # (its only "caller" was data)
    assert (0x0800, 3, "read") in _regions(net, slug)


def test_a_master_system_trace_starts_functions_where_code_ran(tmp_path):
    net, slug = _project(tmp_path, "game.sms", sms_rom(size=0x20000, code=CODE))
    net.import_coverage(slug, b"00:0700\n00:0702\n", "game.trace")
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    assert 0x0700 in _starts(net, slug)


NES = {0x0000: bytes.fromhex("4c0080"),  # reset: jmp $8000
       0x0100: bytes.fromhex("a90160"),  # lda #1 · rts: nothing reaches it
       0x0200: bytes.fromhex("200083"),  # three bytes of data that read like `jsr $8300`
       0x0300: bytes.fromhex("a9028510 60")}  # lda #2 · sta $10 · rts


def test_an_nes_log_from_mesen2_or_fceux_is_read_by_program_rom_offset(tmp_path):
    data = nes_rom(prg_units=2, chr_units=1, code=NES, vectors=(0x8000, 0x8000, 0x8000))
    prg, chr_ = data[16:16 + 0x8000], data[16 + 0x8000:]
    net, slug = _project(tmp_path, "game.nes", data)
    assert 16 + 0x0100 not in _starts(net, slug) and 16 + 0x0300 in _starts(net, slug)
    log = cdl(prg, {0x0100: 0x09, 0x0101: 0x01, 0x0102: 0x01, 0x0200: 0x02, 0x0201: 0x02, 0x0202: 0x02})
    with pytest.raises(NetworkError, match="another version"):
        net.import_coverage(slug, log[:5] + b"\0\0\0\0" + log[9:], "game.cdl")
    net.import_coverage(slug, log, "game.cdl")
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    starts = _starts(net, slug)
    assert 16 + 0x0100 in starts and 16 + 0x0300 not in starts
    assert (16 + 0x0200, 3, "read") in _regions(net, slug)
    # FCEUX's: program and character ROM, no header, no entries: code that ran still starts a function
    fceux = bytearray(len(prg) + len(chr_))
    fceux[0x0100:0x0103] = b"\x01\x01\x01"
    cov = coverage.read_cdl(bytes(fceux), prg, {zlib.crc32(prg)}, fceux=len(prg) + len(chr_))
    assert cov.entries == set() and cov.code[0x0100]
    net.import_coverage(slug, bytes(fceux), "game.cdl")
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    assert 16 + 0x0100 in _starts(net, slug)


def test_an_nes_trace_is_refused_for_now(tmp_path):
    net, slug = _project(tmp_path, "game.nes", nes_rom(prg_units=2, code=NES, vectors=(0x8000, 0x8000, 0x8000)))
    with pytest.raises(NetworkError, match="code/data log"):
        net.import_coverage(slug, b"00:8100\n", "game.trace")
