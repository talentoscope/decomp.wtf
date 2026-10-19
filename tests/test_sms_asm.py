"""The assembly track's judge for the Master System and Game Gear (rom/sms_asm.py), in WLA-DX: a documented routine is
accepted when it rebuilds the ROM's bytes, survives the shift test, names banked code from the right bank, keeps
instructions as instructions, names the hardware (sms_hw) and has real names and a header comment."""

from __future__ import annotations

import pytest

from decompnet.rom import sms_asm

pytestmark = pytest.mark.skipif(not sms_asm.available(), reason="needs WLA-DX")

# ld a,($c0a2) · and a · jr z,+15 · ld hl,$3800 · call $0f31 · ld a,$81 · out ($bf),a · ld a,5 · ld ($ffff),a · ret
# (bank 5, running at $8A10 in slot 2)
CODE = bytes.fromhex("3aa2c0 a7 280f 210038 cd310f 3e81 d3bf 3e05 32ffff c9")
HEADER = ("; Clears the name table while a menu is open, then turns the\n"
          "; screen on and maps bank 5 into slot 2. In: nothing. Out: A, HL.\n")
GOOD = HEADER + """ClearMenuMap:
    ld a, (wMenuOpen)
    and a
    jr z, @done
    ld hl, $3800 ; number: the name table's address in video memory
    call FillNameTable
    ld a, $81
    out (PortVDPControl), a
    ld a, 5
    ld (MapperSlot2), a
@done:
    ret
"""


def routine(**kw) -> sms_asm.Routine:
    base = dict(name="func_005_8a10", bank=5, cpu=0x8A10, code=CODE,
                symbols={"wMenuOpen": 0xC0A2, "FillNameTable": 0x0F31, "func_005_8a10": 0x8A10},
                functions={0x0F31: "FillNameTable", 0x14A10: "func_005_8a10"},
                runs_at={0x0F31: 0x0F31, 0x14A10: 0x8A10})
    return sms_asm.Routine(**(base | kw))


def problems(text: str, **kw) -> list[str]:
    return sms_asm.judge(routine(**kw), text).problems


def test_a_documented_routine_is_accepted():
    v = sms_asm.judge(routine(), GOOD)
    assert v.ok, v.problems
    assert v.label == "ClearMenuMap" and v.same == v.size == len(CODE)


def test_the_bytes_must_be_the_roms():
    out = problems(GOOD.replace("ld a, $81", "ld a, $80"))
    assert out and out[0].startswith("1. the bytes differ at $8A1C") and "ld a, $81" in out[0]
    assert problems(GOOD.replace("    ret\n", ""))[0].startswith("1. it assembles to 21 bytes, the routine is 22")
    assert "isn't a name the project has" in problems(GOOD.replace("FillNameTable", "FillTheTable"))[0]


@pytest.mark.parametrize("old, new, check", [
    ("ld a, (wMenuOpen)", "ld a, ($c0a2)", "2. line 4"),  # a raw RAM address
    ("call FillNameTable", "call $0f31", "2. line 8"),  # a raw call target
    ("$3800 ; number: the name table's address in video memory", "$3800", "2. line 7"),  # a constant not marked
    ("out (PortVDPControl), a", "out ($bf), a", "5. line 10"),  # a port by number
    ("ld (MapperSlot2), a", "ld ($ffff), a", "5. line 12"),  # the mapper by address
])
def test_addresses_and_hardware_must_be_named(old, new, check):
    out = problems(GOOD.replace(old, new))
    assert any(p.startswith(check) for p in out), out


def test_a_name_defined_for_the_project_moves_with_it():
    text = ".DEFINE wMenuFlag $C0A2\n" + GOOD.replace("wMenuOpen", "wMenuFlag")
    v = sms_asm.judge(routine(symbols={"FillNameTable": 0x0F31}), text)
    assert v.ok, v.problems
    assert v.defines == {"wMenuFlag": 0xC0A2} and ".DEFINE" not in v.text
    assert any("isn't taken here" in p for p in problems(".DEFINE wMenuFlag wMenuOpen+1\n" + GOOD))
    out = problems(".DEFINE wMenuOpen $C0A3\n" + GOOD)
    assert out == ["2. wMenuOpen already names another address in the project"]


def test_automatic_names_and_a_missing_header_are_refused():
    out = problems(GOOD.replace("ClearMenuMap", "func_005_8a10").replace("@done", "@l_8a25"))
    assert sum(p.startswith("6.") for p in out) == 2
    assert any("comment lines" in p for p in problems(GOOD.replace(HEADER, "")))
    assert any("maps a bank" in p for p in problems(GOOD.replace("maps bank 5 into slot 2", "does the rest  ")))


def test_what_reads_files_or_changes_the_assembly_is_refused():
    for line in ('.INCLUDE "/etc/passwd"', '.INCBIN "rom.sms"', ".MACRO X", ".BANK 0 SLOT 0", ".ORG $0000"):
        out = problems(line + "\n" + GOOD)
        assert out and "isn't taken here" in out[0], (line, out)
    assert not sms_asm.contained('.INCBIN "rom.sms"\n' + GOOD) and sms_asm.contained(GOOD)


def test_data_is_only_where_the_map_has_it():
    assert any(p.startswith("4. line 5") for p in problems(GOOD.replace("    and a\n", "    .db $a7\n")))
    assert sms_asm.judge(routine(data=[(0x14A13, 1, "inline")]), GOOD.replace("    and a\n", "    .db $a7\n")).ok


def test_banked_calls_name_the_bank_that_is_mapped():
    code = bytes.fromhex("cd0040 c9")  # call $4000 · ret, from bank 0
    text = HEADER.replace("maps bank 5 into slot 2", "starts the level") + "StartLevel:\n    call LevelInit\n    ret\n"
    funcs = {0x4000: "LevelInit", 0xC000: "TitleInit", 0x150: "StartLevel"}
    kw = dict(code=code, bank=0, cpu=0x150, symbols={"LevelInit": 0x4000, "TitleInit": 0x4000}, functions=funcs,
              runs_at={0x4000: 0x4000, 0xC000: 0x4000, 0x150: 0x150})
    assert any("isn't known yet" in p for p in problems(text, **kw))  # banks 1 and 3 both have $4000
    # from slot 1 itself: its own bank
    own = dict(kw, bank=1, cpu=0x4100, functions={**funcs, 0x4100: "StartLevel"}, runs_at={**kw["runs_at"], 0x4100: 0x4100})
    assert sms_asm.judge(routine(**own), text).ok
    out = problems(text.replace("LevelInit", "TitleInit"), **own)
    assert any(p.startswith("3.") and "TitleInit is in bank 3" in p for p in out)


def test_the_draft_rebuilds_the_bytes_and_leaves_the_writing():
    text = sms_asm.draft(routine())
    v = sms_asm.judge(routine(), text)
    assert v.same == v.size == len(CODE)
    assert all(p.startswith(("2.", "6.")) for p in v.problems), v.problems  # what's left: names and words
    assert "call FillNameTable" in text and "ld a, (wMenuOpen)" in text and "out (PortVDPControl), a" in text
    assert "ld (MapperSlot2), a" in text and "@l_8a25:" in text and "jr z, @l_8a25" in text


def test_a_relative_jump_out_of_the_routine_names_where_it_goes():
    """WLA-DX's jr takes a label: the project's functions are labels where they run."""
    code = bytes.fromhex("3e01 184c")  # ld a,1 · jr $0250   (bank 0, $0200)
    text = HEADER.replace("maps bank 5 into slot 2", "finishes the frame") + "SetFlag:\n    ld a, 1\n    jr Finish\n"
    kw = dict(code=code, bank=0, cpu=0x200, symbols={"Finish": 0x250}, functions={0x250: "Finish", 0x200: "func_000_0200"},
              runs_at={0x250: 0x250, 0x200: 0x200})
    assert sms_asm.judge(routine(**kw), text).ok
    out = problems(text.replace("jr Finish", "jr $0250-CADDR-1"), **kw)
    assert out == ["2. line 5: `jr $0250-CADDR-1`: $0250 is an address left as a number: name it (a label, or the "
                   "project's name for the function there)"]
