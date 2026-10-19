"""Following Master System code further (rom/sms_trace.py): a function that ends in a jump to one that returns
returns too; a computed call comes back to the address it pushed; a table a function indexes itself is read as far
as its index is bounded; slot 0 is paged by 0xFFFD; a bank kept in RAM, read back from the mapper or saved on the
stack is followed through; and a symbol file's automatic names seed code without naming it."""

from __future__ import annotations

from decompnet.rom import sms, sms_trace

from .test_sms import make_rom

SIZE = 0x20000  # 8 banks: the Sega mapper pages slot 2
BANK5 = {0x14000: bytes.fromhex("3e07c9")}  # bank 5 at 0x8000: ld a,7 · ret
TO_BANK5 = "3e05" "32ffff" "cd0080"  # ld a,5 · ld ($ffff),a · call $8000: reached only by following the code here


def starts(code: dict) -> dict:
    return sms.candidates(make_rom(size=SIZE, code={0x0000: bytes.fromhex("c30001"), **code}))[0]


def test_a_function_that_ends_in_a_jump_to_one_that_returns_returns():
    found = starts({
        0x0100: bytes.fromhex("cd5002" "cd0002" + TO_BANK5 + "18fe"),  # call $0250 · call $0200 · (bank 5) · loop
        0x0200: bytes.fromhex("3e01" "c35002"),  # ld a,1 · jp $0250: its tail is $0250's
        0x0250: bytes.fromhex("c9"),
        **BANK5})
    assert found[0x14000] == (0x8000, "call")  # so its caller went on after calling it


def test_a_computed_call_comes_back_to_the_address_it_pushed():
    rom = make_rom(size=SIZE, code={
        0x0000: bytes.fromhex("cd0001" "18fe"),  # call $0100 · loop
        0x0100: bytes.fromhex("210002" "110801" "d5" "e9" + TO_BANK5 + "c9"),  # ld hl,$0200 · ld de,$0108 · push de ·
        0x0200: bytes.fromhex("c9"),  # jp (hl) · (on at $0108) ...
        **BANK5})
    found, _ = sms.candidates(rom)
    assert found[0x14000] == (0x8000, "call") and found[0x0200][1] == "call"
    assert 0x0108 not in found  # it's the rest of the function at $0100, not one of its own
    fn = next(f for f in sms.functions(rom) if f.rom == 0x0100)
    assert len(fn.code) == 0x0108 + 9 - 0x0100


def test_push_then_ret_is_a_jump():
    found = starts({0x0100: bytes.fromhex("010003" "c5" "c9"),  # ld bc,$0300 · push bc · ret
                    0x0300: bytes.fromhex("3e02c9")})
    assert found[0x0300] == (0x0300, "jump")


INDEXED = "3a00c0" + "{guard}" + "875f1600" "21{table}" "19" "7e23666f" "e9"
# ld a,($c000) · (the guard) · add a,a · ld e,a · ld d,0 · ld hl,table · add hl,de · ld a,(hl) · inc hl ·
# ld h,(hl) · ld l,a · jp (hl)
HANDLERS = {0x0400: bytes.fromhex("3e01c9"), 0x0420: bytes.fromhex("3e02c9"), 0x0440: bytes.fromhex("3e03c9")}


def test_a_table_indexed_after_a_bound_is_read_that_far():
    """`cp 3 / ret nc`: three entries (a 0 one among them, no handler); the word after them isn't the table's."""
    found = starts({
        0x0100: bytes.fromhex("cd0002" "18fe"),
        0x0200: bytes.fromhex(INDEXED.format(guard="fe03d0", table="0005")),  # cp 3 · ret nc
        0x0500: bytes.fromhex("0004" "0000" "2004" "4004"),
        **HANDLERS})
    assert found[0x0400][1] == found[0x0420][1] == "table"
    assert 0x0440 not in found


def test_a_table_indexed_without_a_bound_is_read_up_to_what_isnt_code():
    found = starts({
        0x0100: bytes.fromhex("cd0002" "18fe"),
        0x0200: bytes.fromhex(INDEXED.format(guard="", table="0005")),
        0x0500: bytes.fromhex("0004" "2004" "0000" "4004"),  # (a 0 past the first entry ends it)
        **HANDLERS})
    assert found[0x0400][1] == found[0x0420][1] == "table" and 0x0440 not in found


def test_a_table_of_jumps_is_read_as_code():
    found = starts({
        0x0100: bytes.fromhex("cd0002" "18fe"),
        0x0200: bytes.fromhex(INDEXED.format(guard="fe02d0", table="0005").replace("7e23666f", "")),  # jp (hl) into it
        0x0500: bytes.fromhex("c30004" "c32004" "c34004"),  # jp $0400 · jp $0420 · jp $0440 (two of them are its)
        **HANDLERS})
    assert found[0x0500][1] == found[0x0503][1] == "table" and found.get(0x0506, (0, ""))[1] != "table"
    assert 0x0400 in found and 0x0420 in found


def test_a_conditional_branch_keeps_the_table_base_and_index_bound_distinct():
    rom = make_rom(size=SIZE, code={
        0x0000: bytes.fromhex("cd0002" "18fe"),
        # Bound the index, compute a table address, then branch over a return to read the handler.
        0x0200: bytes.fromhex("3a00c0" "fe03d0" "875f1600" "210005" "19" "2801" "c9" "7e23666f" "e9"),
        0x0500: bytes.fromhex("0004" "2004" "4004" "6004"),
        **HANDLERS, 0x0460: bytes.fromhex("3e04c9"),
    })
    found = sms_trace.Tracer(rom).follow({0: (0, "vector")})
    assert all(found.starts[at][1] == "table" for at in HANDLERS)
    assert 0x0460 not in found.starts  # a valid-looking pointer immediately after the bounded table


def test_slot_0_is_paged_above_its_first_kib():
    found = starts({0x0100: bytes.fromhex("3e04" "32fdff" "cd0010" "18fe"),  # ld a,4 · ld ($fffd),a · call $1000
                    0x11000: bytes.fromhex("3e04c9")})  # bank 4's $1000
    assert found[0x11000] == (0x1000, "call")


def test_a_bank_kept_in_ram_is_followed_through():
    found = starts({0x0100: bytes.fromhex("3e05" "3200c1"  # ld a,5 · ld ($c100),a   (the game's own copy)
                                          "3e00" "3a00c1" "32ffff" "cd0080" "18fe"),  # ld a,0 · ld a,($c100) · map it
                    **BANK5})
    assert found[0x14000] == (0x8000, "call")


def test_a_bank_saved_from_the_mapper_and_restored_is_followed_through():
    found = starts({0x0100: bytes.fromhex(TO_BANK5 + "3affff" "f5"  # (bank 5) · ld a,($ffff) · push af
                                          "3e06" "32ffff" "cd0080"  # ld a,6 · ld ($ffff),a · call $8000 (bank 6)
                                          "f1" "32ffff" "cd0081" "18fe"),  # pop af · ld ($ffff),a · call $8100 (5)
                    **BANK5, 0x18000: bytes.fromhex("3e08c9"), 0x14100: bytes.fromhex("3e09c9")})
    assert found[0x18000] == (0x8000, "call") and found[0x14100] == (0x8100, "call")


def test_ram_mirror_readback_can_differ_from_the_selected_bank():
    found = starts({
        0x0100: bytes.fromhex("3e05" "32ffff" "3e06" "32ffdf" "cd0080"
                              "3affff" "32ffff" "cd0080" "18fe"),
        **BANK5, 0x18000: bytes.fromhex("3e08c9"),
    })
    # The alias store changes readback to 6 while bank 5 remains selected for the first call.
    assert found[0x14000] == (0x8000, "call")
    assert found[0x18000] == (0x8000, "call")


def test_mapper_register_reads_require_ram_evidence_instead_of_reset_bank_fallback():
    rom = make_rom(size=SIZE, code={0: bytes.fromhex("3affff" "32ffff" "cd0080" "18fe"),
                                   0x8000: bytes.fromhex("3e02c9")})
    found = sms_trace.Tracer(rom).follow({0: (0, "vector")})
    assert 0x8000 not in found.starts


def test_indirect_work_ram_writes_and_reads_use_the_same_alias():
    found = starts({0x0100: bytes.fromhex("2100c1" "3605" "2100e1" "7e" "32ffff" "cd0080" "18fe"),
                    **BANK5})
    assert found[0x14000] == (0x8000, "call")


def test_ex_sp_hl_first_thing_takes_the_return_address():
    rom = make_rom(size=SIZE, code={0x0000: bytes.fromhex("cd0002" "05" "18fe"),  # call $0200 · db 5 · ...
                                    0x0200: bytes.fromhex("e3" "7e" "23" "e3" "c9")})  # ex (sp),hl · ld a,(hl) · ...
    found = sms_trace.Tracer(rom).follow({0: (0, "vector")})
    assert 0x0200 in found.takes_return


def test_a_symbol_files_automatic_names_seed_code_but_dont_name_it(tmp_path):
    rom = tmp_path / "game.sms"
    rom.write_bytes(make_rom(size=SIZE, code={0x0000: bytes.fromhex("18fe"), 0x0600: bytes.fromhex("3e01c9"),
                                              0x0700: bytes.fromhex("3e02c9")}))
    (tmp_path / "game.sym").write_text("[labels]\n0000:0600 _LABEL_600_\n0000:0700 SetUp\n")
    fns = {f.rom: f.name for f in sms.split(rom, tmp_path, log=lambda *_: None)}
    assert fns[0x0600] == "func_000_0600" and fns[0x0700] == "SetUp"
    assert sms_trace.read_sym("[labels]\n0000:0600 _LABEL_600_\n") == {}
