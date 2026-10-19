"""The assembly track's judge for the NES (rom/nes_asm.py), in ca65: a documented routine is accepted when it rebuilds
the ROM's bytes (zero-page addresses in the 3-byte form written `a:`), survives the shift test, names banked code from
the right bank, keeps instructions as instructions, names the hardware (nes_hw) and has real names and a header."""

from __future__ import annotations

import pytest

from decompnet.rom import nes_asm

pytestmark = pytest.mark.skipif(not nes_asm.available(), reason="needs cc65")

# lda $10 · beq +17 · lda #$80 · sta $2000 · jsr $c123 · lda $0300 · sta $8000 · ldx $0010 · rts
# (MMC3, bank 5 at $8A10)
CODE = bytes.fromhex("a510 f011 a980 8d0020 2023c1 ad0003 8d0080 ae1000 60")
HEADER = ("; Turns rendering on when the menu flag is set, calls the sound\n"
          "; update and maps the menu's bank. In: nothing. Out: A, X trashed.\n")
GOOD = HEADER + """StartMenu:
    lda zMenuOpen
    beq @done
    lda #$80
    sta PPUCTRL
    jsr UpdateSound
    lda wMenuBank
    sta MMC3_BANK_SELECT
    ldx a:zMenuOpen
@done:
    rts
"""
FLAT = 16 + 5 * 0x2000 + 0x0A10
SOUND = 16 + 14 * 0x2000 + 0x0123  # (the fixed bank at $C000)


def routine(**kw) -> nes_asm.Routine:
    base = dict(name="func_005_8a10", bank=5, cpu=0x8A10, code=CODE,
                symbols={"zMenuOpen": 0x10, "wMenuBank": 0x0300, "UpdateSound": 0xC123, "func_005_8a10": 0x8A10},
                functions={FLAT: "func_005_8a10", SOUND: "UpdateSound"}, runs_at={FLAT: 0x8A10, SOUND: 0xC123},
                banks={FLAT: 5, SOUND: 14}, flat=FLAT, window=0x2000, mapper=4)
    return nes_asm.Routine(**(base | kw))


def problems(text: str, **kw) -> list[str]:
    return nes_asm.judge(routine(**kw), text).problems


def test_a_documented_routine_is_accepted():
    v = nes_asm.judge(routine(), GOOD)
    assert v.ok, v.problems
    assert v.label == "StartMenu" and v.same == v.size == len(CODE)


def test_the_bytes_must_be_the_roms_and_zero_page_in_three_bytes_says_a():
    out = problems(GOOD.replace("lda #$80", "lda #$90"))
    assert out[0].startswith("1. the bytes differ at $8A14") and "lda #$80" in out[0]
    out = problems(GOOD.replace("ldx a:zMenuOpen", "ldx zMenuOpen"))
    assert out[0].startswith("1.") and "`a:` keeps the 3-byte form" in out[0]


@pytest.mark.parametrize("old, new, check", [
    ("lda zMenuOpen", "lda $10", "2. line 4"),            # zero page by number
    ("lda wMenuBank", "lda $0300", "2. line 9"),          # RAM by number
    ("jsr UpdateSound", "jsr $c123", "2. line 8"),        # a call by number
    ("sta PPUCTRL", "sta $2000", "5. line 7"),            # a PPU register by number
    ("sta MMC3_BANK_SELECT", "sta $8000", "5. line 10"),  # the mapper by number
])
def test_addresses_and_hardware_must_be_named(old, new, check):
    out = problems(GOOD.replace(old, new))
    assert any(p.startswith(check) for p in out), out


def test_names_defined_for_the_project_move_with_it():
    text = "zMenuFlag = $10\n" + GOOD.replace("zMenuOpen", "zMenuFlag")
    v = nes_asm.judge(routine(symbols={"wMenuBank": 0x0300, "UpdateSound": 0xC123}), text)
    assert v.ok, v.problems
    assert v.defines == {"zMenuFlag": 0x10} and "zMenuFlag =" not in v.text
    assert problems("zMenuOpen = $11\n" + GOOD) == ["2. zMenuOpen already names another address in the project"]


def test_automatic_names_a_missing_header_and_refused_directives():
    out = problems(GOOD.replace("StartMenu", "func_005_8a10").replace("@done", "@l_8a25"))
    assert sum(p.startswith("6.") for p in out) == 2
    assert any("comment lines" in p for p in problems(GOOD.replace(HEADER, "")))
    for line in ('.include "x.s"', '.incbin "rom.nes"', ".macro x", '.segment "X"', ".org $8000"):
        out = problems(line + "\n" + GOOD)
        assert out and "isn't taken here" in out[0], (line, out)


def test_data_is_only_where_the_map_has_it():
    as_data = GOOD.replace("    lda #$80\n", "    .byte $a9, $80\n")
    assert any(p.startswith("4. line 6") for p in problems(as_data))
    assert nes_asm.judge(routine(data=[(FLAT + 4, 2, "table")]), as_data).ok


def test_a_branch_out_of_the_routine_names_where_it_goes():
    code = bytes.fromhex("a510 d010 60")  # lda $10 · bne $c014 · rts   (fixed bank, $c000)
    flat = 16 + 14 * 0x2000
    text = HEADER.replace("maps the menu's bank", "goes on to the next") + "Check:\n    lda zMenuOpen\n    bne Next\n    rts\n"
    kw = dict(code=code, bank=14, cpu=0xC000, flat=flat, symbols={"zMenuOpen": 0x10, "Next": 0xC014},
              functions={flat: "func_014_c000", flat + 0x14: "Next"}, runs_at={flat: 0xC000, flat + 0x14: 0xC014},
              banks={flat: 14, flat + 0x14: 14})
    assert nes_asm.judge(routine(**kw), text).ok
    out = problems(text.replace("bne Next", "bne $c014"), **kw)
    assert any(p.startswith("2. line 5") for p in out), out


def test_the_draft_rebuilds_the_bytes_and_leaves_the_writing():
    text = nes_asm.draft(routine())
    v = nes_asm.judge(routine(), text)
    assert v.same == v.size == len(CODE)
    assert all(p.startswith(("2.", "6.")) for p in v.problems), v.problems
    for want in ("lda zMenuOpen", "sta PPUCTRL", "jsr UpdateSound", "lda wMenuBank", "sta MMC3_BANK_SELECT",
                 "ldx a:zMenuOpen", "beq @l_8a25", "@l_8a25:"):
        assert want in text, (want, text)


def test_brk_and_an_indirect_jump_through_zero_page_draft_as_ca65_takes_them():
    code = bytes.fromhex("0000 6c2e00")  # brk (its byte) · jmp ($002e)
    r = routine(code=code, cpu=0xC000, bank=14, flat=16 + 14 * 0x2000, symbols={}, functions={}, runs_at={}, banks={})
    text = nes_asm.draft(r)
    assert "    brk\n    .byte $00\n" in text and "jmp ($002e)" in text
    v = nes_asm.judge(r, text)
    assert v.same == v.size == len(code) and not any(p.startswith("4.") for p in v.problems), v.problems
