"""Following NES code past the places a straight sweep misreads (rom/nes.py): a jump engine's table after each call
to it is its handlers, not instructions; a routine that reads bytes after its call ends its caller's straight line
there; a branch that's always taken ends it as a jmp does; and a function skipped into through a `bit` operand
doesn't cut the one around it short."""

from __future__ import annotations

from decompnet.rom import nes

from .test_nes import make_rom

# SMB's jump engine: asl a · tay · pla · sta $04 · pla · sta $05 · iny · lda ($04),y · sta $06 · iny · lda ($04),y ·
# sta $07 · jmp ($0006)
ENGINE = bytes.fromhex("0aa8688504688505c8b1048506c8b10485076c0600")
HANDLER = bytes.fromhex("a90160")  # lda #1 · rts


def _layout(rom: bytes):
    h = nes.parse_header(rom)
    return nes.layout_of(h), rom[h["prg_off"]:h["prg_off"] + h["prg_size"]]


def test_a_jump_engines_table_is_its_handlers():
    code = {0x00: bytes.fromhex("a510" "2080c0" "a0c0" "b0c0" "c0c0"),  # lda $10 · jsr engine · .dw $c0a0,$c0b0,$c0c0
            0x80: ENGINE, 0xA0: HANDLER, 0xB0: HANDLER, 0xC0: HANDLER}
    rom = make_rom(code=code, vectors=(0xC000, 0xC000, 0xC000))
    layout, prg = _layout(rom)
    engines: dict = {}
    found = nes.candidates(prg, layout, engines=engines)
    assert engines == {0x80: "engine"}
    assert {found[0xA0], found[0xB0], found[0xC0]} == {"table"}
    assert not any(0x05 <= p < 0x0B for p in found)  # nothing starts in the table
    report: dict = {}
    fns = {f.vram: f for f in nes.functions(rom, report=report)}
    assert fns[0xC000].code == bytes.fromhex("a5102080c0")  # the caller ends at the call
    assert fns[0xC080].code == ENGINE
    assert (16 + 0x05, 6, "table") in [r[:3] for r in report["regions"]]  # (file offsets: after the 16-byte header)


def test_a_routine_that_reads_what_follows_its_call_ends_its_callers_line_there():
    reader = bytes.fromhex("6885046885050a" "a001b104" "60")  # pla · sta $04 · pla · sta $05 · asl a · ldy #1 · lda ($04),y · rts
    code = {0x00: bytes.fromhex("2040c0" "07" "60"), 0x40: reader}  # jsr reader · .db 7 · ...
    rom = make_rom(code=code, vectors=(0xC000, 0xC000, 0xC000))
    layout, prg = _layout(rom)
    engines: dict = {}
    nes.candidates(prg, layout, engines=engines)
    assert engines == {0x40: "inline"}
    fns = {f.vram: f for f in nes.functions(rom)}
    assert fns[0xC000].code == bytes.fromhex("2040c0")


def test_restoring_registers_from_the_stack_isnt_reading_the_return_address():
    rom = make_rom(code={0x00: bytes.fromhex("2040c060"), 0x40: bytes.fromhex("68a868aa6860")},  # pla·tay·pla·tax·pla·rts
                   vectors=(0xC000, 0xC000, 0xC000))
    layout, prg = _layout(rom)
    assert nes.reads_return(prg, layout, 0x40) is None


def test_a_branch_that_is_always_taken_ends_the_line_as_a_jmp_does():
    assert nes._always("sec", "bcs $c010") and nes._always("bcc $c004", "bcs $c010")
    assert nes._always("lda #$00", "beq $c010") and nes._always("ldx #$05", "bne $c010")
    assert not nes._always("lda $10", "beq $c010") and not nes._always("lda #$00", "bne $c010")
    code = {0x00: bytes.fromhex("a510" "18" "9002" "0212" "60")}  # lda $10 · clc · bcc +2 · (2 bytes of data) · rts
    rom = make_rom(code=code, vectors=(0xC000, 0xC000, 0xC000))
    fns = {f.vram: f for f in nes.functions(rom)}
    assert fns[0xC000].code == bytes.fromhex("a510189002")  # (not on into the data)
    assert fns[0xC007].code == bytes.fromhex("60")  # where the branch goes: a function of its own


def test_a_function_skipped_into_through_a_bit_operand_doesnt_cut_the_one_around_it():
    # reset: jsr $c010 · jsr $c013 · jmp $c006;  $c010: lda #1 · bit $02a9 · sta $10 · rts  ($c013 is the bit's operand:
    # lda #2 there)
    code = {0x00: bytes.fromhex("2010c0" "2013c0" "4c06c0"), 0x10: bytes.fromhex("a9012ca902851060")}
    rom = make_rom(code=code, vectors=(0xC000, 0xC000, 0xC000))
    fns = {f.vram: f for f in nes.functions(rom)}
    assert fns[0xC010].code == bytes.fromhex("a9012ca902851060")  # all of it, to its rts
    assert fns[0xC013].code == bytes.fromhex("a902851060")
