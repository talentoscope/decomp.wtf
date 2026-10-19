"""The Z80 disassembler (rom/z80.py): known encodings, control-flow classification, the scoring adapter, and every
encoding round-tripped through SDCC's own assembler (sdasz80) when the toolchain is installed."""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from decompnet.rom import gb_build, z80

needs_toolchain = pytest.mark.skipif(not gb_build.available("z80"),
                                     reason="the Z80 C toolchain (gbdk-2020) is not installed")


@pytest.mark.parametrize("hexbytes, text, length", [
    ("3e11", "ld a, $11", 2), ("c9", "ret", 1), ("cd0640", "call $4006", 3), ("00", "nop", 1),
    ("c3 3412", "jp $1234", 3), ("fe 20", "cp $20", 2), ("d3 be", "out ($be), a", 2), ("db bf", "in a, ($bf)", 2),
    ("21 00c0", "ld hl, $c000", 3), ("3a 34c2", "ld a, ($c234)", 3), ("32 ffff", "ld ($ffff), a", 3),
    ("e5", "push hl", 1), ("f1", "pop af", 1), ("08", "ex af, af'", 1), ("d9", "exx", 1), ("eb", "ex de, hl", 1),
    ("e9", "jp (hl)", 1), ("f9", "ld sp, hl", 1), ("ff", "rst $38", 1), ("76", "halt", 1), ("f3", "di", 1),
    ("cb 7f", "bit 7, a", 2), ("cb 86", "res 0, (hl)", 2), ("cb 36", "sll (hl)", 2),
    ("ed b0", "ldir", 2), ("ed 56", "im 1", 2), ("ed 44", "neg", 2), ("ed 4d", "reti", 2), ("ed 45", "retn", 2),
    ("ed 52", "sbc hl, de", 2), ("ed 43 3412", "ld ($1234), bc", 4), ("ed 7b 3412", "ld sp, ($1234)", 4),
    ("ed 78", "in a, (c)", 2), ("ed 79", "out (c), a", 2), ("ed 47", "ld i, a", 2),
    ("dd 21 3412", "ld ix, $1234", 4), ("fd 7e 05", "ld a, (iy+$05)", 3), ("dd 77 fb", "ld (ix-$05), a", 3),
    ("dd 36 02 10", "ld (ix+$02), $10", 4), ("dd e9", "jp (ix)", 2), ("dd e5", "push ix", 2),
    ("dd 09", "add ix, bc", 2), ("dd 29", "add ix, ix", 2), ("dd 22 3412", "ld ($1234), ix", 4),
    ("dd 44", "ld b, ixh", 2), ("fd 6d", "ld iyl, iyl", 2), ("dd 66 07", "ld h, (ix+$07)", 3),
    ("dd cb 03 46", "bit 0, (ix+$03)", 4), ("fd cb fe de", "set 3, (iy-$02)", 4),
    ("dd cb 05 06", "rlc (ix+$05)", 4), ("dd cb 05 00", "rlc (ix+$05), b", 4),
])
def test_known_encodings(hexbytes, text, length):
    raw = bytes.fromhex(hexbytes.replace(" ", ""))
    assert z80.decode(raw + b"\x00\x00", 0x1000) == (text, length)
    assert z80.decode(raw, 0x1000) == (text, length)  # exactly the instruction, nothing after it


def test_relative_jumps_show_the_address_they_reach():
    assert z80.decode(bytes.fromhex("18fe"), 0x0100) == ("jr $0100", 2)  # to itself
    assert z80.decode(bytes.fromhex("2005"), 0x0100) == ("jr nz, $0107", 2)
    assert z80.decode(bytes.fromhex("10f0"), 0x0100) == ("djnz $00f2", 2)  # backwards
    assert z80.decode(bytes.fromhex("3801"), 0x7ffe) == ("jr c, $8001", 2)


def test_what_is_not_an_instruction_is_data_and_never_loses_alignment():
    assert z80.decode(b"\xdd\x00", 0) == ("db $dd", 1)  # a prefix that changes nothing is a one-byte nop
    assert z80.decode(b"\xfd\xfd\x21\x00\x00", 0) == ("db $fd", 1)
    assert z80.decode(b"\xed\x00", 0) == ("db $ed, $00", 2)  # an undefined ED opcode is a two-byte nop
    assert z80.decode(b"\xed", 0) == ("db $ed", 1)  # cut off by the end of the buffer
    assert z80.decode(b"\x3e", 0) == ("db $3e", 1)
    assert z80.decode(b"", 0) == ("db $00", 1)
    assert z80.decode(b"\xdd\xcb\x01", 0) == ("db $dd", 1)


def test_every_first_byte_decodes_to_something_of_a_sane_length():
    for op in range(256):
        for rest in (b"\x00\x00\x00", b"\xcb\x00\x00", b"\x7f\x7f\x7f"):
            text, ln = z80.decode(bytes([op]) + rest, 0x4000)
            assert text and 1 <= ln <= 4, (op, text, ln)


def test_control_flow_is_classified_the_way_function_discovery_needs():
    assert z80.flow("ret") == ("ret", None) and z80.flow("reti") == ("ret", None) and z80.flow("jp (hl)") == ("ret", None)
    assert z80.flow("ret nc") == ("cret", None)
    assert z80.flow("jp $1234") == ("jump", 0x1234) and z80.flow("jr $0100") == ("jump", 0x0100)
    assert z80.flow("jp nz, $1234") == ("cjump", 0x1234) and z80.flow("djnz $0100") == ("cjump", 0x0100)
    assert z80.flow("call $4006") == ("call", 0x4006) and z80.flow("call z, $4006") == ("call", 0x4006)
    assert z80.flow("rst $38") == ("call", 0x38) and z80.flow("ld a, b") == ("plain", None)
    # only an unconditional return or jump ends a function
    assert [z80.is_return(t) for t in ("ret", "reti", "jp $1234", "jr $0100", "jp (hl)")] == [True] * 5
    assert [z80.is_return(t) for t in ("ret nc", "ret z", "jr nz, $0100", "jp c, $1234", "djnz $0100", "call $4006")] == [False] * 6


def test_the_scoring_adapter_reads_z80_the_way_scoring_reads_capstone():
    insns = list(z80.Decoder().disasm(bytes.fromhex("3e11c9cd0640"), 0x100))
    assert [(i.mnemonic, i.op_str) for i in insns] == [("ld", "a, $11"), ("ret", ""), ("call", "$4006")]
    assert all(i.id for i in insns) and [i.size for i in insns] == [2, 1, 3] and insns[2].address == 0x103
    assert insns[2].group(None) is True and insns[0].group(None) is False
    assert list(z80.Decoder().disasm(b"\xdd\x00", 0))[0].id == 0  # data has id 0, as capstone gives undecodable bytes


# ------------------------------------------------------------------ every encoding, through SDCC's assembler

# sdasz80 writes a few instructions another way than the CPU reads them: it picks the canonical opcode
_ALIAS_ED = {0x4C, 0x54, 0x5C, 0x64, 0x6C, 0x74, 0x7C, 0x55, 0x5D, 0x65, 0x6D, 0x75, 0x7D, 0x4E, 0x66, 0x6E, 0x76,
             0x7E, 0x77, 0x7F, 0x63, 0x6B}


def _sdas(text: str) -> str:
    """The same instruction in sdasz80's syntax: immediates take `#`, indexed operands are written `d (ix)`."""
    mnemonic, _, ops = text.partition(" ")
    ops = re.sub(r"\((ix|iy)([+-])\$([0-9a-f]{2})\)", lambda m: f"{'-' if m[2] == '-' else ''}0x{m[3]} ({m[1]})", ops)
    ops = re.sub(r"\(\$([0-9a-f]{2,4})\)", lambda m: f"(0x{m[1]})", ops)
    if mnemonic in ("jp", "call", "jr", "djnz", "rst"):
        ops = re.sub(r"\$([0-9a-f]{2,4})", lambda m: f"0x{m[1]}", ops)
    else:
        ops = re.sub(r"(?<![\w(+x])\$([0-9a-f]{2,4})", lambda m: f"#0x{m[1]}", ops)
    return f"{mnemonic} {ops}".strip()


@needs_toolchain
def test_every_encoding_round_trips_through_sdasz80():
    """decode(bytes) -> text -> sdasz80 -> bytes must give the bytes back, for every opcode form the assembler accepts
    (jr/djnz are left to the next test: sdasz80 measures an absolute target differently)."""
    pad = bytes([0x12, 0x34, 0x56])
    seqs = [bytes([op]) + pad for op in range(256)]
    seqs += [bytes([pre, op]) + pad for pre in (0xCB, 0xED, 0xDD, 0xFD) for op in range(256)]
    seqs += [bytes([pre, 0xCB, 0x05, op]) + pad for pre in (0xDD, 0xFD) for op in range(256)]
    lines, expect = [".area TESTABS (ABS)", ".allow_undocumented"], []
    for seq in seqs:
        text, ln = z80.decode(seq, 0x1000)
        if text.startswith(("db ", "jr ", "jr", "djnz")) or text.split()[0] in ("jr", "djnz"):
            continue
        lines += [".org 0x1000", f"\t{_sdas(text)}"]
        expect.append((len(lines), seq[:ln], text))
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "t.asm").write_text("\n".join(lines) + "\n")
        run = subprocess.run([str(gb_build.toolchain_bin("sdasz80")), "-l", "-o", "t.rel", "t.asm"], cwd=tmp,
                             capture_output=True, text=True)
        listing = (Path(tmp) / "t.lst").read_text()
    rejected = {int(m[1]) for m in re.finditer(r"t\.asm:(\d+):", run.stdout + run.stderr)}
    got = {}
    for line in listing.splitlines():
        m = re.match(r"^\s*[0-9A-Fa-f]{8}\s+((?:[0-9A-Fa-f]{2}\s+)+)\s*\[\s*\d+\]\s+(\d+)\s+", line)
        if m:
            got[int(m[2])] = bytes(int(b, 16) for b in m[1].split())
    compared = bad = 0
    for ln, raw, text in expect:
        if ln in rejected or ln not in got:  # the assembler does not take some undocumented forms
            continue
        if raw[0] == 0xED and raw[1] in _ALIAS_ED:
            continue
        if raw[0] in (0xDD, 0xFD) and len(raw) == 4 and raw[1] == 0xCB and (raw[3] >> 6) == 1 and (raw[3] & 7) != 6:
            continue  # bit n,(ix+d) is encoded the same whatever the register field says
        compared += 1
        if raw != got[ln]:
            bad += 1
    assert compared > 700 and bad == 0, (compared, bad)


@needs_toolchain
def test_relative_jumps_agree_with_sdasz80(tmp_path):
    (tmp_path / "j.asm").write_text(".area TESTABS (ABS)\n.org 0x1000\n\tjr fwd\n\t.blkb 0x12\nfwd:\n\tdjnz fwd\n"
                                    "\tjr nz, back\n\tjr c, fwd\nback:\n\tjr back\n")
    subprocess.run([str(gb_build.toolchain_bin("sdasz80")), "-l", "-o", "j.rel", "j.asm"], cwd=tmp_path, check=True,
                   capture_output=True)
    code = bytes.fromhex("1812") + bytes(0x12) + bytes.fromhex("10fe2002" "38fa" "18fe")
    texts = [t for _, t, _ in z80.disassemble(code, 0x1000) if t.split()[0] in ("jr", "djnz")]
    assert texts == ["jr $1014", "djnz $1014", "jr nz, $101a", "jr c, $1014", "jr $101a"]
    listing = (tmp_path / "j.lst").read_text()
    assert "18 12" in listing and "10 FE" in listing and "20 02" in listing and "38 FA" in listing and "18 FE" in listing
