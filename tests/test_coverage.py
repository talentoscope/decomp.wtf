"""Coverage from an emulator (rom/coverage.py): a Mesen2 code/data log or an execution trace says what ran as code,
what was read as data and where functions were entered; following a Game Boy game's code starts from it, and splitting
the game again uses the one the user imported."""

from __future__ import annotations

import zlib

import pytest

from decompnet.network import NetworkError
from decompnet.rom import coverage, gb_mgbdis, gb_trace
from tests.test_corrections import game  # noqa: F401 (a fixture)
from tests.test_gb_map import rom_with

needs_tools = pytest.mark.skipif(not gb_mgbdis.available(), reason="needs mgbdis and RGBDS")


def cdl(rom: bytes, marks: dict[int, int], header: bool = True, crc: int | None = None) -> bytes:
    flags = bytearray(len(rom))
    for at, f in marks.items():
        flags[at] |= f
    return (b"CDLv2" + (zlib.crc32(rom) if crc is None else crc).to_bytes(4, "little") if header else b"") + bytes(flags)


def test_a_code_data_log_says_what_ran_what_was_read_and_where_calls_entered():
    rom = rom_with({0x150: "af c9"})
    cov = coverage.read_cdl(cdl(rom, {0x150: 0x09, 0x151: 0x01, 0x4000: 0x02, 0x4001: 0x02, 0x4002: 0x03}), rom)
    assert cov.entries == {0x150} and cov.code[0x150] and cov.code[0x151]
    assert cov.runs("read") == [(0x4000, 2)]  # (0x4002 also ran: code, not data)
    assert cov.summary() == {"kind": "cdl", "code": 3, "read": 2, "entries": 1}
    assert coverage.read_cdl(cdl(rom, {0x150: 0x01}, header=False), rom).code[0x150]  # an older log, no header


@pytest.mark.parametrize("raw, says", [(b"CDLv2" + b"\0" * 4 + b"\x01" * 16, "covers 16 bytes"),
                                       (b"hello", "not a code/data log")])
def test_a_log_that_isnt_for_this_game_is_refused(raw, says):
    with pytest.raises(coverage.CoverageError, match=says):
        coverage.read_cdl(raw, rom_with({}))


def test_a_log_for_another_version_or_with_no_code_is_refused():
    rom = rom_with({})
    with pytest.raises(coverage.CoverageError, match="another version"):
        coverage.read_cdl(cdl(rom, {0x150: 1}, crc=1234), rom)
    with pytest.raises(coverage.CoverageError, match="no code"):
        coverage.read_cdl(cdl(rom, {0x4000: 2}), rom)


def _trace(rom: bytes, text: str) -> coverage.Coverage:
    tracer = gb_trace.Tracer(rom)

    def decode(flat):
        text, ln = tracer.decode(flat)
        return (text, ln, *gb_trace.sm83.flow(text))

    return coverage.read_trace(text, rom, lambda b, c: c if c < 0x4000 else b * 0x4000 + c - 0x4000 if b else None, decode)


def test_an_execution_trace_marks_code_and_the_calls_that_ran():
    rom = rom_with({0x150: "cd0040 18fe", 0x8000: "3e01 c9"})  # bank 2's 0x4000
    cov = _trace(rom, "00:0150\n02:4000\n02:4002\n; a comment\n00:0153\n")
    assert cov.code[0x150:0x153] == b"\x01" * 3 and cov.code[0x8000] and cov.code[0x8002]
    assert cov.entries == {0x8000}  # the call at 0x150 ran, and $4000 ran from bank 2 only
    with pytest.raises(coverage.CoverageError, match="bank:address"):
        _trace(rom, "nothing here\n")


def test_following_starts_from_what_ran_and_leaves_what_was_read():
    """A handler only reached through a pointer the code builds at run time: the log saw it entered."""
    rom = rom_with({0x150: "18fe", 0x4200: "3e01 c9", 0x4300: "3e02 c9"})  # 0x4300 decodes as code, but it's read
    cov = coverage.read_cdl(cdl(rom, {0x4200: 0x09, 0x4201: 1, 0x4202: 1, 0x4300: 2, 0x4301: 2, 0x4302: 2}), rom)
    hints = {"coverage": cov}
    found = gb_trace.follow(rom, hints)
    assert found.starts[0x4200][1] == "ran" and 0x4300 not in found.code
    regs = gb_trace.regions(rom, found, hints)
    assert (0x4300, 3, "read", "read as data while the game ran (coverage)") in regs


def test_code_that_ran_is_never_data():
    rom = rom_with({0x150: "18fe", 0x4400: "d3 dd 3e01 c9"})  # bytes that read as no code would, but they ran
    cov = coverage.read_cdl(cdl(rom, {k: 1 for k in range(0x4400, 0x4406)}), rom)
    regs = gb_trace.regions(rom, gb_trace.follow(rom, {"coverage": cov}), {"coverage": cov})
    assert not any(kind == "data" and lo <= 0x4400 < lo + n for lo, n, kind, _ in regs)


def test_the_project_keeps_the_coverage_the_user_imports(game):  # noqa: F811
    net, _ = game
    proj = net._project("g")
    rom = (net._sym_path(proj)).with_suffix(".gbc").read_bytes()
    out = net.import_coverage("g", cdl(rom, {0x150: 0x09, 0x151: 1}), "game.cdl")
    assert out["entries"] == 1 and out["from"] == "game.cdl"
    assert net._coverage_path(proj).suffix == ".cdl"
    assert net._hints(proj)["coverage"].entries == {0x150}
    assert net.corrections("g")["coverage"]["code"] == 2
    with pytest.raises(NetworkError, match="another version"):
        net.import_coverage("g", cdl(rom, {0x150: 1}, crc=99), "old.cdl")
    assert net._coverage_path(proj).suffix == ".cdl"  # the one it had stays
    net.import_coverage("g", b"00:0150\n", "run.trace")
    assert net._coverage_path(proj).suffix == ".trace" and net.corrections("g")["coverage"]["kind"] == "trace"
    net.remove_coverage("g")
    assert net._coverage_path(proj) is None and "coverage" not in net._hints(proj)


@needs_tools
def test_splitting_again_follows_the_code_the_game_ran(tmp_path):
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    net.auto_install_tools = False
    rom = rom_with({0x150: "18fe", 0x4200: "3e01 c9"})
    path = tmp_path / "game.gb"
    path.write_bytes(rom)
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    net.import_coverage(slug, cdl(rom, {0x4200: 0x09, 0x4201: 1, 0x4202: 1}), "game.cdl")
    net.resplit_banked(slug, apply=True, log=lambda *_: None)
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND rom_offset=?", net._project(slug)["id"], 0x4200)
    assert fn is not None and fn["size"] == 3
