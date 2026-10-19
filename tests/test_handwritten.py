"""Code no compiler makes (handwritten.py): CodeWarrior's runtime helpers, which take their input in r11/r12 (each
Mario Party 6 module links copies of them), are taken out of agents' way; compiled code never is."""

import struct
import subprocess

import pytest

from decompnet import toolchains
from decompnet.app.twins import object_prints
from decompnet.handwritten import ppc_reason


def code(*words: int) -> bytes:
    return b"".join(struct.pack(">I", w) for w in words)


PTMF_THUNK = code(0x800C0000, 0x816C0004, 0x818C0008, 0x2C0B0000, 0x7C630214)  # lwz r0,0(r12)...: fn_1_E428
COPY = code(0x80040000, 0x7C001A14, 0x90050000, 0x7CA32B78, 0x4E800020)  # lwz r0,0(r4); add r0,r0,r3...: fn_1_E4E0


def test_runtime_helpers_are_assembly():
    assert ppc_reason(PTMF_THUNK).startswith("reads r12 before setting it")
    assert ppc_reason(code(0x91CBFFB8)).startswith("reads r11 before setting it")  # stw r14,-72(r11): __save_gpr
    assert ppc_reason(code(0x7D8903A6, 0x4E800420)).startswith("reads r12")  # mtctr r12; bctr


def test_compiled_code_is_not():
    assert ppc_reason(COPY) is None  # r0 is set before it's read
    # stwu r1; mflr r0; stw r0; bl: after a call (or any branch) nothing is looked at, r12 included
    assert ppc_reason(code(0x9421FFF0, 0x7C0802A6, 0x90010014, 0x48000001, 0x818C0008)) is None
    assert ppc_reason(code(0x60000000, 0x38000000, 0x80600010, 0x4E800020)) is None  # nop; li r0,0; lwz r3,16(0)
    assert ppc_reason(code(0x7C0004AC, 0x4E800020)) is None  # sync: C has it too, from the SDK's inline helpers
    assert ppc_reason(b"") is None and ppc_reason(code(0)) is None


@pytest.mark.skipif(toolchains.installed("gc-wii-binutils") is None, reason="needs the GameCube binutils")
def test_fingerprints_say_which_code_is_assembly(tmp_path):
    (tmp_path / "x.s").write_text("""
    .text
    .globl __ptmf_scall
    .type __ptmf_scall, @function
__ptmf_scall:
    lwz r0, 0(r12)
    lwz r11, 4(r12)
    lwz r12, 8(r12)
    add r3, r3, r0
    cmpwi r11, 0
    blt 1f
    lwzx r12, r3, r12
    lwzx r12, r12, r11
1:  mtctr r12
    bctr
    .size __ptmf_scall, .-__ptmf_scall
    .globl copy
    .type copy, @function
copy:
    lwz r0, 0(r4)
    add r0, r0, r3
    stw r0, 0(r5)
    mr r3, r5
    blr
    .size copy, .-copy
""")
    gas = toolchains.path("gc-wii-binutils") / "powerpc-eabi-as"
    subprocess.run([str(gas), "-mgekko", "-mregnames", "-o", str(tmp_path / "x.o"), str(tmp_path / "x.s")], check=True)
    prints = object_prints((tmp_path / "x.o").read_bytes())
    assert prints["__ptmf_scall"].asm.startswith("reads r12") and prints["copy"].asm == ""


def le(*words: int) -> bytes:
    return b"".join(struct.pack("<I", w) for w in words)


def test_mips_code_no_compiler_makes():
    """#22 (a PS1 game): agents spent their turns on a one-instruction piece that runs into the next function, and on
    PSY-Q's libgte (ctc2: the GTE, which C can't drive without inline asm)."""
    from decompnet.handwritten import mips_reason

    assert mips_reason(le(0x8D110024), False, "psx").startswith("ends without a return")  # lw s1, 0x24(t0)
    assert "GTE" in mips_reason(le(0x48C4D000, 0x03E00008, 0), False, "psx")  # ctc2 a0, $26; jr ra; nop
    assert "coprocessor 0" in mips_reason(le(0x40026000, 0x03E00008, 0), False, "n64")  # mfc0 v0, Status
    assert mips_reason(le(0x8D020000, 0x03E00008, 0), False, "psx").startswith("reads t0")  # lw v0, 0(t0); jr ra
    # compiled code: a prologue, a call, the epilogue; on the PS2, t0-t3 carry arguments
    prologue = le(0x27BDFFE8, 0xAFBF0010, 0x0C000000, 0, 0x8FBF0010, 0x03E00008, 0x27BD0018)
    assert mips_reason(prologue, False, "psx") is None
    assert mips_reason(le(0x8D020000, 0x03E00008, 0), False, "ps2") is None  # lw v0, 0(t0) on the PS2: an argument
    assert mips_reason(le(0x03E00008, 0, 0, 0), False, "psx") is None  # alignment padding after jr ra


def test_game_boy_code_sdcc_never_writes():
    """A Game Boy game written in assembly: its split handed agents padding, graphics, and routines that run into the
    next one. Byte sequences from a GBC game's split (bank switches in high RAM at $ff9b)."""
    from decompnet.handwritten import sm83_reason

    h = bytes.fromhex
    assert sm83_reason(h("00")).startswith("padding") and sm83_reason(h("ffff00")).startswith("padding")
    assert sm83_reason(h("c9"), capped=True).startswith("the split ran 1 KiB")
    assert "no instruction" in sm83_reason(h("3e01d3c9"))  # D3: no SM83 instruction has it
    assert "SDCC never writes" in sm83_reason(h("40c9"))  # ld b, b: tile data
    assert "SDCC never writes" in sm83_reason(h("3e01ffc9"))  # rst $38: an FF byte
    assert sm83_reason(h("3e01ea00c0")).startswith("runs on")  # ld a, 1; ld [$c000], a: and on into the next
    assert "carry flag" in sm83_reason(h("3802c9c9"))  # jr c first: a carry its caller left
    assert "zero flag" in sm83_reason(h("c8af c9".replace(" ", "")))  # ret z first
    assert "carry flag" in sm83_reason(h("ce01c9"))  # adc a, 1 first
    assert "stack pointer" in sm83_reason(h("3100e0c9"))  # ld sp, $e000
    assert sm83_reason(h("76c9")).startswith("halt")
    # what SDCC does write: a bank-switching wrapper, a flag set before it's read, a loop, a tail jump
    wrapper = h("f09be09c3e14e09bea0021cdef4ef09ce09bea0021c9")
    assert sm83_reason(wrapper, 0x198) is None
    assert sm83_reason(h("fe053801c9c9")) is None  # cp 5; jr c to the second ret
    assert sm83_reason(h("21ff9b0100043e10320b78b120f8c9"), 0x2997) is None  # a fill loop
    assert sm83_reason(h("c34729")) is None  # jp $2947


def test_a_routine_the_split_ran_on_ends_where_it_does():
    from decompnet.handwritten import sm83_routine

    h = bytes.fromhex
    wrapper = h("f09be09c3e14e09bea0021cdef4ef09ce09bea00210600" "0e68c9")
    assert sm83_routine(wrapper + h("000000") + h("3e08cd7235"), 0x198) == len(wrapper)  # then padding, more code
    assert sm83_routine(h("2001c9afc900"), 0) is None  # jr nz over the ret: reads the zero flag (hand-written)
    assert sm83_routine(h("1802c900afc9" "3e01"), 0) == 6  # jr over the first ret: it goes on to the second
    assert sm83_routine(h("3e01c9"), 0) is None  # nothing follows: nothing to cut
    assert sm83_routine(h("d3c9"), 0) is None  # data from the start
    assert sm83_routine(h("4000c9" "00"), 0) is None  # ld b, b: not a routine SDCC could write


def test_what_sdcc_never_writes_on_the_game_boy_as_measured():
    """Measured with GBDK-2020 4.5 (SDCC 4.5.1, -msm83): arguments come in a, e, de and bc, never hl (and
    __z88dk_fastcall doesn't exist there); a call at the end becomes a jp, conditional ones too; jr stays inside the
    function. An interrupt handler pushes hl first: saving it isn't reading it."""
    from decompnet.handwritten import sm83_reason

    h = bytes.fromhex
    assert "reads hl" in sm83_reason(h("7ec9"), 0x100)  # ld a, (hl); ret: a pointer in hl
    assert "reads hl" in sm83_reason(h("77c9"), 0x100)  # ld (hl), a
    assert "reads l" in sm83_reason(h("2cc9"), 0x100)  # inc l
    assert "then ret" in sm83_reason(h("cd4729c9"), 0x100)  # call $2947; ret: SDCC writes jp $2947
    assert "leaves the function" in sm83_reason(h("b728f0c9c9"), 0x100)  # or a; jr z back into the routine before
    # what SDCC writes
    assert sm83_reason(h("1213c9"), 0x100) is None  # ld (de), a; inc de: arguments in a and de
    assert sm83_reason(h("f8027eea00c0c9"), 0x100) is None  # __sdcccall(0): ldhl sp, #2 first
    assert sm83_reason(h("f5c5d5e521ffc03601e1d1c1f1d9"), 0x100) is None  # an __interrupt handler's push hl
    assert sm83_reason(h("b7caef4ec9"), 0x100) is None  # or a; jp z, _gv; ret: a conditional tail call
    assert sm83_reason(h("b72801c9afc9"), 0x100) is None  # a jr inside the function
    assert sm83_reason(h("b72003cd4729c9"), 0x100) is None  # call; ret, with a branch to that ret: SDCC may


def test_what_the_nes_and_master_system_splits_hand_out_that_no_c_is():
    """Their splits start a function wherever code branches to, so hand-written routines come out in pieces that run
    on into the next one; and data. A whole function (ending on a return or a jump) is left alone."""
    from decompnet.handwritten import mos6502_reason, z80_reason

    h = bytes.fromhex
    assert "runs on" in mos6502_reason(h("48"), 0x8000)  # pha, then the next piece
    assert "runs on" in mos6502_reason(h("a9008d0620"), 0x8000)  # lda #0; sta $2006
    assert "no instruction" in mos6502_reason(h("a90002"), 0x8000)
    assert "brk" in mos6502_reason(h("a9000000"), 0x8000)  # zero bytes
    assert mos6502_reason(h("0000"), 0x8000).startswith("padding")
    assert "longest" in mos6502_reason(h("60"), 0x8000, capped=True)
    assert mos6502_reason(h("60"), 0x8000) is None  # rts
    assert mos6502_reason(h("ad2105cd2505f003"), 0x8000) is not None  # (cut off before its return)
    assert mos6502_reason(h("ad2105cd2505f0038d25052061fe60"), 0x8000) is None  # a whole routine
    assert mos6502_reason(h("4ca1fe"), 0x8000) is None  # a tail call
    assert "runs on" in z80_reason(h("eda0eda0"), 0x0a95)  # ldi; ldi: an unrolled copy's middle
    assert "halt" in z80_reason(h("76c9"), 0x100)
    assert "no instruction" in z80_reason(h("edffc9"), 0x100)
    assert z80_reason(h("ffff"), 0x100).startswith("padding")
    assert z80_reason(h("3a06cad3bfc9"), 0x100) is None  # ld a, ($ca06); out ($bf), a; ret
    assert z80_reason(h("b7ca0040c9"), 0x100) is None  # a conditional tail call, then ret
    assert z80_reason(h("c30040"), 0x100) is None  # jp
