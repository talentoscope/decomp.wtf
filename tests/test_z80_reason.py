"""What SDCC never writes on the Z80 (handwritten.z80_reason): a Master System routine that does one of these is
hand-written, set aside from the C track (and handed to the assembly track); SDCC's own shapes aren't."""

from __future__ import annotations

import pytest

from decompnet.handwritten import z80_reason

H = bytes.fromhex


@pytest.mark.parametrize("code, says", [
    ("d9 c9", "alternate registers"),                       # exx · ret
    ("08 c9", "alternate registers"),                       # ex af,af' · ret
    ("dd7e05 c9", "reads ix before setting it"),           # ld a,(ix+5) · ret: a pointer kept in ix
    ("10fe c9", "reads b before setting it"),              # djnz $ · ret
    ("ed79 c9", "reads c before setting it"),              # out (c),a · ret
    ("2001 c9 c9", "reads a flag before setting it"),      # jr nz,+1 · ret · ret
    ("e17e23e5c9", "return address"),                       # pop hl · ld a,(hl) · inc hl · push hl · ret
    ("3100df c9", "stack pointer"),                         # ld sp,$df00 · ret
    ("ed56 c9", "setting up the CPU"),                      # im 1 · ret
    ("cd0020 c9", "SDCC writes a call at the end as a jp"),  # call $2000 · ret
    ("18f0", "leaves the function"),                         # jr back out of it
])
def test_what_sdcc_never_writes(code, says):
    why = z80_reason(H(code.replace(" ", "")), 0x1000)
    assert why and says in why, why


@pytest.mark.parametrize("code", [
    "7d87c9",                    # ld a,l · add a,a · ret
    "e1c1e9",                    # pop hl · pop bc · jp (hl): SDCC dropping its stack arguments on return
    "a5 4f fd210200 fd39 fd7e00 b1 e1c1e9",  # and l · ld c,a · ld iy,2 · add iy,sp · ... · pop hl · pop bc · jp (hl)
    "dde5dd210000dd39 dd6e04 dde1 c9",       # push ix · ld ix,0 · add ix,sp · ld l,(ix+4) · pop ix · ret
    "0610 3e00 10fe c9",          # ld b,16 · ld a,0 · djnz $ · ret
])
def test_sdccs_own_shapes_stay_on_the_c_track(code):
    assert z80_reason(H(code.replace(" ", "")), 0x1000) is None
