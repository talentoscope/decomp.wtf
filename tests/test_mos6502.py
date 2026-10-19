"""The 6502 decoder (rom/mos6502.py): every opcode against capstone's MOS65XX back end, and the control-flow reading
the function sweep depends on."""

from __future__ import annotations

import re

import capstone
import pytest

from decompnet.rom import mos6502


def _numbers(text: str) -> list[int]:
    return [int(x, 16) for x in re.findall(r"(?:0x|\$)([0-9a-fA-F]+)", text)]


def test_every_opcode_agrees_with_capstone():
    md = capstone.Cs(capstone.CS_ARCH_MOS65XX, capstone.CS_MODE_MOS65XX_6502)
    for op in range(256):
        code = bytes([op, 0x34, 0x12])
        ours, length = mos6502.decode(code, 0x8000)
        theirs = list(md.disasm(code, 0x8000, 1))
        if not theirs:
            assert ours == f"db ${op:02x}" and length == 1, f"opcode {op:02x}: capstone has none, we decoded {ours!r}"
            continue
        c = theirs[0]
        mnemonic, _, operands = ours.partition(" ")
        assert (mnemonic, length, _numbers(operands)) == (c.mnemonic, c.size, _numbers(c.op_str)), \
            f"opcode {op:02x}: ours {ours!r} ({length}), capstone {c.mnemonic} {c.op_str} ({c.size})"


def test_the_table_holds_the_151_documented_opcodes():
    assert len(mos6502.OPCODES) == 151


@pytest.mark.parametrize("code,text", [
    ("a900", "lda #$00"), ("a510", "lda $10"), ("ad0020", "lda $2000"), ("b510", "lda $10,x"),
    ("b90030", "lda $3000,y"), ("a110", "lda ($10,x)"), ("b110", "lda ($10),y"), ("6c3412", "jmp ($1234)"),
    ("0a", "asl a"), ("200080", "jsr $8000"), ("60", "rts"), ("ea", "nop"),
])
def test_instruction_text(code, text):
    assert mos6502.decode(bytes.fromhex(code), 0x9000)[0] == text


def test_a_branch_shows_its_absolute_target():
    assert mos6502.decode(bytes.fromhex("d0fe"), 0x8000) == ("bne $8000", 2)      # a branch to itself
    assert mos6502.decode(bytes.fromhex("f005"), 0x8000) == ("beq $8007", 2)
    assert mos6502.decode(bytes.fromhex("90f0"), 0x8010) == ("bcc $8002", 2)      # backwards
    assert mos6502.decode(bytes.fromhex("d003"), 0xFFFC)[0] == "bne $0001"        # wraps at the top of memory


def test_an_undefined_opcode_or_a_cut_short_instruction_is_one_data_byte():
    assert mos6502.decode(b"\x02", 0) == ("db $02", 1)
    assert mos6502.decode(b"\xad\x00", 0) == ("db $ad", 1)   # a three-byte instruction with one byte left
    assert mos6502.decode(b"", 0) == ("db $00", 1)


def test_flow_classifies_what_a_function_sweep_needs():
    flow = mos6502.flow
    assert flow("rts") == ("ret", None) and flow("rti") == ("ret", None) and flow("brk $00") == ("ret", None)
    assert flow("jmp $c123") == ("jump", 0xC123) and flow("jmp ($fffc)") == ("ret", None)
    assert flow("jsr $8123") == ("call", 0x8123)
    assert flow("bne $8005") == ("cjump", 0x8005)
    assert flow("lda $2002") == ("plain", None)
    assert mos6502.is_return("rts") and mos6502.is_return("jmp $8000") and not mos6502.is_return("beq $8000")


def test_disassemble_walks_a_buffer_and_never_stops_short():
    code = bytes.fromhex("a900" "8d0020" "d0fb" "02" "60")
    out = list(mos6502.disassemble(code, 0xC000))
    assert [(a, t) for a, t, _ in out] == [(0xC000, "lda #$00"), (0xC002, "sta $2000"), (0xC005, "bne $c002"),
                                          (0xC007, "db $02"), (0xC008, "rts")]
    assert mos6502.asm_text(code, 0xC000).splitlines()[0] == "c000  lda #$00"


def test_the_scoring_adapter_looks_like_capstone():
    ins = list(mos6502.Decoder().disasm(bytes.fromhex("20008060"), 0x8000))
    assert [(i.address, i.size, i.mnemonic, i.op_str) for i in ins] == [(0x8000, 3, "jsr", "$8000"), (0x8003, 1, "rts", "")]
    assert all(i.group(None) for i in ins) and ins[0].bytes == bytes.fromhex("200080") and ins[0].id == 1
