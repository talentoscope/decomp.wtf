"""The assembly track's judge (rom/gb_asm.py): a documented Game Boy routine is accepted when it rebuilds the ROM's
bytes, survives the shift test, names banked code from the right bank, keeps instructions as instructions, names the
hardware, and has real names and a header comment."""

from __future__ import annotations

import pytest

from decompnet.rom import gb_asm

pytestmark = pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")

# ld a,[$c0a2] · and a · jr z,+10 · ld hl,$9800 · call $0f31 · ld a,$81 · ldh [$ff40],a · ret  (bank 2, $4a10)
CODE = bytes.fromhex("faa2c0 a7 280a 210098 cd310f 3e81 e040 c9")
HEADER = "; Clears the background map while a menu\n; is open, then turns the screen back on.\n; In: nothing. Out: A, HL trashed.\n"
GOOD = HEADER + """ClearMenuBackground:
    ld a, [wMenuOpen]
    and a
    jr z, .done
    ld hl, TILEMAP0
    call FillTilemapZero
    ld a, LCDC_ON | LCDC_BG_ON
    ldh [rLCDC], a
.done:
    ret
"""


def routine(**kw) -> gb_asm.Routine:
    base = dict(name="Call_002_4a10", bank=2, cpu=0x4A10, code=CODE,
                symbols={"wMenuOpen": 0xC0A2, "FillTilemapZero": 0x0F31, "Call_002_4a10": 0x4A10},
                functions={0x0F31: "FillTilemapZero", 0x8A10: "Call_002_4a10"})
    return gb_asm.Routine(**(base | kw))


def problems(text: str, **kw) -> list[str]:
    return gb_asm.judge(routine(**kw), text).problems


def test_a_documented_routine_is_accepted():
    v = gb_asm.judge(routine(), GOOD)
    assert v.ok, v.problems
    assert v.label == "ClearMenuBackground" and v.same == v.size == len(CODE)


def test_the_bytes_must_be_the_roms():
    out = problems(GOOD.replace("LCDC_ON | LCDC_BG_ON", "LCDC_ON"))
    assert out and out[0].startswith("1. the bytes differ at $4A1C") and "ld a, $81" in out[0]
    assert problems(GOOD.replace("    ret\n", ""))[0].startswith("1. it assembles to 16 bytes, the routine is 17")


@pytest.mark.parametrize("old, new, check", [
    ("ld a, [wMenuOpen]", "ld a, [$c0a2]", "2. line 5"),  # a raw RAM address
    ("call FillTilemapZero", "call $0f31", "2. line 9"),  # a raw call target
    ("ld hl, TILEMAP0", "ld hl, $9800", "5. line 8"),  # video memory by address
    ("ldh [rLCDC], a", "ldh [$ff40], a", "5. line 11"),  # a register by address
])
def test_addresses_and_hardware_must_be_named(old, new, check):
    out = problems(GOOD.replace(old, new))
    assert any(p.startswith(check) for p in out), out


def test_a_name_that_only_hides_an_address_is_still_an_address():
    """`ld a, [$c000 + $a2]` doesn't move with the shift test; DEF naming the address does."""
    assert any(p.startswith("2. line 5") for p in problems(GOOD.replace("[wMenuOpen]", "[$c000 + $a2]")))
    text = "DEF wMenuFlag EQU $c0a2\n" + GOOD.replace("wMenuOpen", "wMenuFlag")
    v = gb_asm.judge(routine(symbols={"FillTilemapZero": 0x0F31}), text)
    assert v.ok, v.problems
    assert v.defines == {"wMenuFlag": 0xC0A2} and "DEF wMenuFlag" not in v.text


def test_a_constant_marked_as_a_number_isnt_an_address():
    code = bytes.fromhex("010002 c9")  # ld bc, $0200 · ret
    text = HEADER + "CopyLength:\n    ld bc, $0200\n    ret\n"
    assert any(p.startswith("2.") for p in problems(text, code=code, cpu=0x4000, symbols={}, functions={}))
    marked = text.replace("$0200\n", "$0200 ; number: bytes to copy\n")
    assert gb_asm.judge(routine(code=code, cpu=0x4000, symbols={}, functions={}), marked).ok


def test_automatic_names_and_a_missing_header_are_refused():
    out = problems(GOOD.replace("ClearMenuBackground", "Call_002_4a10").replace(".done", ".label_4a20"))
    assert sum(p.startswith("6.") for p in out) == 2
    assert any("comment lines" in p for p in problems(GOOD.replace(HEADER, "")))


def test_what_reads_files_or_changes_the_assembly_is_refused():
    for line in ('INCLUDE "/etc/passwd"', 'INCBIN "rom.gbc"', "SECTION \"x\", ROM0", "DEF X EQUS \"ld\"", "OPT b.X"):
        out = problems(line + "\n" + GOOD)
        assert out and "isn't taken here" in out[0], (line, out)


def test_data_is_only_where_the_map_has_it():
    as_db = GOOD.replace("    and a\n", "    db $a7\n")
    assert any(p.startswith("4. line 6") for p in problems(as_db))
    # a far call's operands after it: data in the map, so db and dw are right there
    code = bytes.fromhex("cd0006 03 0041 c9")  # call FarCall · db 3 · dw $4100 · ret  (bank 0, $0200)
    text = HEADER + "CallBankThree:\n    call FarCall\n    db 3\n    dw BankThreeInit\n    ret\n"
    syms = {"FarCall": 0x0600, "BankThreeInit": 0x4100}
    kw = dict(code=code, bank=0, cpu=0x0200, symbols=syms, functions={0x600: "FarCall", 0xC100: "BankThreeInit"})
    assert any(p.startswith("4.") for p in problems(text, **kw))
    assert gb_asm.judge(routine(**kw, data=[(0x203, 3, "inline")]), text).ok


def test_banked_calls_name_the_bank_that_is_mapped():
    code = bytes.fromhex("cd0040 c9")  # call $4000 · ret, from bank 0
    text = HEADER + "StartLevel:\n    call LevelInit\n    ret\n"
    fns = {0x8000: "LevelInit", 0xC000: "TitleInit", 0x150: "StartLevel"}
    kw = dict(code=code, bank=0, cpu=0x150, symbols={"LevelInit": 0x4000, "TitleInit": 0x4000}, functions=fns)
    assert any("isn't known yet" in p for p in problems(text, **kw))  # banks 2 and 3 both have $4000
    assert gb_asm.judge(routine(**kw, edges={0: 0x8000}), text).ok  # the split followed it into bank 2
    out = problems(text.replace("LevelInit", "TitleInit"), **kw, edges={0: 0x8000})
    assert any(p.startswith("3.") and "TitleInit is in bank 3" in p for p in out)


def test_the_draft_rebuilds_the_bytes_and_leaves_the_writing():
    v = gb_asm.judge(routine(), gb_asm.draft(routine()))
    assert v.same == v.size == len(CODE)  # it assembles to the routine
    assert all(p.startswith(("2.", "6.")) for p in v.problems), v.problems  # what's left: names and words
    text = gb_asm.draft(routine())
    assert "call FillTilemapZero" in text and "ld a, [wMenuOpen]" in text and "ldh [rLCDC], a" in text
    assert ".l_4a20:" in text and "ld hl, TILEMAP0" in text


def test_a_label_the_project_has_for_something_else_is_refused():
    """In the exported build it would be two things at once."""
    out = problems(GOOD.replace("ClearMenuBackground:", "FillTilemapZero:"))
    assert out and out[0].startswith("6. FillTilemapZero already names something else")
    assert gb_asm.judge(routine(symbols={"ClearMenuBackground": 0x4A10, "wMenuOpen": 0xC0A2,
                                         "FillTilemapZero": 0x0F31}), GOOD).ok  # (its own name, at its own address)
