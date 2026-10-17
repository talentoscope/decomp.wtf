import shutil

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import c64, detect
from decompnet.rom.n64 import RomError


def prg(code=b"\xa9\x01\x60", address=0x0801):
    return address.to_bytes(2, "little") + code


def test_prg_identifies_load_address_and_reachable_code():
    program = prg()
    picked = detect.identify(program, "demo.prg")
    assert picked.platform == "c64"
    assert picked.data == program
    assert picked.info["load_address"] == picked.info["entrypoint"] == 0x0801
    functions = c64.split(program)
    assert len(functions) == 1
    assert functions[0].address == 0x0801
    assert functions[0].code == b"\xa9\x01\x60"
    assert c64.assemble(functions[0].asm, functions[0].address) == functions[0].code


def test_split_follows_a_jump_into_the_middle_of_a_linear_sweep():
    # jmp $0805 lands inside the 3-byte "lda $01a9" a linear decode sees at $0804
    program = prg(b"\x4c\x05\x08\xad\xa9\x01\x60")
    functions = c64.split(program)
    assert [(fn.address, fn.n_insns) for fn in functions] == [(0x0801, 3)]
    assert c64.assemble(functions[0].asm, 0x0801) == functions[0].code


def test_split_follows_direct_subroutine_calls():
    program = prg(b"\x20\x05\x08\x60\xa9\x01\x60")
    functions = c64.split(program)
    assert [(fn.address, fn.code) for fn in functions] == [
        (0x0801, b"\x20\x05\x08\x60"),
        (0x0805, b"\xa9\x01\x60"),
    ]
    assert all(c64.assemble(fn.asm, fn.address) == fn.code for fn in functions)


def test_t64_extracts_program_data():
    code = b"\xa9\x01\x60"
    at = 0x60
    tape = bytearray(at + len(code))
    tape[:18] = b"C64 tape image file"
    tape[0x24:0x26] = (1).to_bytes(2, "little")
    entry = memoryview(tape)[0x40:0x60]
    entry[0] = 1
    entry[2:4] = (0x0801).to_bytes(2, "little")
    entry[4:6] = (0x0801 + len(code)).to_bytes(2, "little")
    entry[8:12] = at.to_bytes(4, "little")
    entry[16:20] = b"DEMO"
    entry.release()
    tape[at:] = code
    program, info = c64.read_program(bytes(tape), "demo.t64")
    assert program == prg(code)
    assert info["title"] == "DEMO" and info["container"] == "T64"


def test_assemble_keeps_absolute_zero_page_operands_and_spaced_indexes():
    code = b"\xae\x00\x00\x4d\x12\x00\x55\x55\x00\x00"
    assert c64.assemble(c64.listing(code, 0x1000), 0x1000) == code


def test_folder_import_combines_programs_and_skips_data(tmp_path):
    folder = tmp_path / "prg"
    folder.mkdir()
    (folder / "001 (0800-0802).prg").write_bytes(prg(b"\xa9\x01\x60"))
    (folder / "002 (0800-0802).prg").write_bytes(prg(b"\xa9\x02\x60"))
    (folder / "002 repeat.prg").write_bytes(prg(b"\xa9\x02\x60"))
    (folder / "003 data.prg").write_bytes(prg(bytes(64), 0x2000))
    (folder / "notes.txt").write_text("not a program")
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(folder, confirmed=True, name="My Game", log=lambda *_: None)
    rows = network.store.all("SELECT name, unit, vram, target FROM functions WHERE project_id=? ORDER BY id",
                             network.project_row(slug)["id"])
    assert [(r["name"], r["vram"], r["target"]) for r in rows] == [
        ("func_0801", 0x0801, b"\xa9\x01\x60"), ("func_0801_2", 0x0801, b"\xa9\x02\x60")]
    assert rows[0]["unit"] != rows[1]["unit"]
    agent, _ = network.local_agent("c64-folder")
    for row in rows:
        asm = network.store.one("SELECT asm FROM functions WHERE project_id=? AND name=?",
                                network.project_row(slug)["id"], row["name"])["asm"]
        assert network.compile_and_diff(agent, row["name"], asm, slug).startswith("✅ MATCH")
    network.store.conn.close()


def test_c_compiler_choice_matches_c_with_cc65(tmp_path):
    from decompnet import toolchains

    if toolchains.installed("cc65") is None and shutil.which("cc65") is None:
        pytest.skip("cc65 is not installed")
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(prg(b"\x20\x04\x08\x60\x60"), toolchain="cc65", confirmed=True, name="demo.prg",
                              log=lambda *_: None)
    assert network.overview(slug)["compiler"]["toolchain"] == "cc65"
    agent, _ = network.local_agent("c64-c")
    result = network.compile_and_diff(agent, "func_0804", "void func_0804(void) {}", slug)
    assert result.startswith("✅ MATCH")
    network.set_compiler(slug, "6502-asm")
    assert network.overview(slug)["compiler"]["toolchain"] == "6502-asm"
    with pytest.raises(Exception, match="cc65"):
        network.set_compiler(slug, "gcc-2.7.2-kmc")
    network.store.conn.close()


def test_cc65_project_rejects_assembly(tmp_path):
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(prg(b"\x60\x60"), toolchain="cc65", confirmed=True, name="demo.prg", log=lambda *_: None)
    agent, _ = network.local_agent("c64-asm")
    result = network.compile_and_diff(agent, "func_0801", "rts", slug)
    assert "not assembly" in result and "MATCH" not in result
    network.store.conn.close()


def test_folder_import_follows_calls_between_programs(tmp_path):
    folder = tmp_path / "prg"
    folder.mkdir()
    (folder / "001 main.prg").write_bytes(prg(b"\x20\x02\x20\x60"))
    (folder / "002 lib.prg").write_bytes(prg(b"\x00\x00\xa9\x05\x60", 0x2000))
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(folder, confirmed=True, name="Linked", log=lambda *_: None)
    pid = network.project_row(slug)["id"]
    rows = {r["name"]: r for r in network.store.all("SELECT name, unit, asm FROM functions WHERE project_id=?", pid)}
    assert set(rows) == {"func_0801", "func_2002"}
    assert "; func_2002 in 002-002-lib.prg" in rows["func_0801"]["asm"]
    assert c64.assemble(rows["func_0801"]["asm"], 0x0801) == b"\x20\x02\x20\x60"
    network.store.conn.close()


def test_folder_import_without_code_names_tapclean(tmp_path):
    (tmp_path / "tapes").mkdir()
    (tmp_path / "tapes" / "game.tap").write_bytes(c64._TAP_MAGIC + bytes(16))
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    with pytest.raises(Exception, match="TAPClean"):
        network.import_rom(tmp_path / "tapes", confirmed=True, log=lambda *_: None)
    network.store.conn.close()


def test_tap_is_rejected_with_tapclean_instructions():
    for data in (c64._TAP_MAGIC + bytes(16), b"\0" * 32):
        with pytest.raises(RomError, match="TapClean"):
            detect.identify(data, "demo.tap")


def test_d64_extracts_first_prg_directory_entry():
    code = prg()
    disk = bytearray(174848)
    directory_offset = sum(c64._D64_SECTORS[1:18]) * 256 + 256
    disk[directory_offset:directory_offset + 2] = b"\0\0"
    entry = directory_offset + 2
    disk[entry:entry + 3] = bytes((0x82, 1, 0))
    disk[entry + 3:entry + 7] = b"DEMO"
    file_sector = 0
    disk[file_sector] = 0
    disk[file_sector + 1] = len(code) + 1
    disk[file_sector + 2:file_sector + 2 + len(code)] = code
    program, info = c64.read_program(bytes(disk), "demo.d64")
    assert program == code
    assert info["title"] == "DEMO" and info["container"] == "D64"


def test_crt_extracts_chip_packet():
    code = b"\xa9\x01\x60"
    header = bytearray(0x40)
    header[:16] = c64._CRT_MAGIC
    header[0x10:0x14] = (0x40).to_bytes(4, "big")
    header[0x20:0x24] = b"DEMO"
    chip = bytearray(0x10)
    chip[:4] = b"CHIP"
    chip[4:8] = (0x10 + len(code)).to_bytes(4, "big")
    chip[0x0C:0x0E] = (0x8000).to_bytes(2, "big")
    chip[0x0E:0x10] = len(code).to_bytes(2, "big")
    program, info = c64.read_program(bytes(header + chip + code), "demo.crt")
    assert program == prg(code, 0x8000)
    assert info["title"] == "DEMO" and info["container"] == "CRT"


def test_import_and_assembly_match(tmp_path):
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(prg(), confirmed=True, name="demo.prg", log=lambda *_: None)
    overview = network.overview(slug)
    assert overview["platform"] == "c64" and overview["functions"] == 1
    assert overview["compiler"]["name"] == "MOS 6510 assembler"
    agent, _ = network.local_agent("c64-agent")
    function = network.store.one("SELECT name, asm FROM functions WHERE project_id=?",
                                 network.project_row(slug)["id"])
    result = network.compile_and_diff(agent, function["name"], function["asm"], slug)
    assert result.startswith("✅ MATCH"), result


def test_split_can_add_code_missed_by_indirect_flow(tmp_path):
    network = RomNetwork(tmp_path / "app.db", tmp_path)
    slug = network.import_rom(prg(b"\xa9\x01\x60\xa9\x02\x60"), confirmed=True, name="demo.prg",
                              log=lambda *_: None)
    agent, _ = network.local_agent("c64-splitter")
    result = network.split_function(agent, slug, "func_0801", "0x0804", "func_0804")
    assert "new function func_0804" in result
    row = network._function("func_0804", slug)
    assert row["target"] == b"\xa9\x02\x60"


def test_c64_c_path_requires_cc65_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(c64, "_cc65_tool", lambda _: None)
    try:
        c64.compile_c("int func_0801(void) { return 1; }", "", "func_0801", 0x0801, {})
    except ValueError as e:
        assert "need cc65" in str(e)
    else:
        raise AssertionError("the C path should report its missing compiler")
