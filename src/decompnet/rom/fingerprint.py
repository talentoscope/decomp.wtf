"""Which toolchain built a game, from what its start-up code or runtime leaves in the ROM.

A game written in C (GBDK, cc65) is the C track's: its functions can match, and the match needs the compiler that
built it (or one that writes the same code). Anything else is hand-written assembly: the map's and the assembly
track's. What's read:

- Game Boy: GBDK's start-up code (crt0), version by version. The checks are a port of gbtoolsid's GBDK ones
  (bbbbbr/gbtoolsid, The Unlicense: src/sig_gbdk.c and src/entry_names_gbdk.h).
- Master System, Game Gear: GBDK-2020's crt0 (`di / im 1 / jp $0080`, then `ld sp, $dff0` there), and the SDSC tag
  homebrew carries at $7FE0 (its program's name and author: homebrew, though not which tool).
- NES: GBDK-2020's start-up code at the reset vector, and cc65's runtime (popax and incsp2, wherever they were
  linked, with its own zero-page stack pointer).

Absence of all of these doesn't prove a game is assembly; it's what nearly every commercial game of these consoles is.
"""

from __future__ import annotations

import re

PINNED = "GBDK-2020 4.5.0"  # the C toolchain the banked lanes compile with (toolchains.toml)


def _at(data: bytes, addr: int, pattern: bytes) -> bool:
    return len(data) >= addr + len(pattern) and data[addr:addr + len(pattern)] == pattern


def gbdk_version(rom: bytes) -> str | None:
    """GBDK's version from a Game Boy ROM's crt0, as gbtoolsid tells it (None: not GBDK's)."""
    h = bytes.fromhex
    if _at(rom, 0x80, h("c5d52ab62809e53a6e67e7e12318f3d1c1e1f041e60220faf1d9")) and _at(rom, 0x20, h("e9")):
        return "2020.4.0.0"
    if _at(rom, 0x10, h("80402010080402010102040810204080")):  # GBDK 2.x - GBDK-2020 3.2.0
        if _at(rom, 0x164, h("21ffff0680")):
            return "2020.3.2.0"
        if _at(rom, 0x1C2, h("3e80e002afcd")):
            return "2.9.5 - 2020.3.1.0" if _at(rom, 0x1CA, h("fbcd")) else "2.9.0 - 2.9.4"
        if _at(rom, 0x1C2, h("3e80e002afea")):
            return "2.0.18 - 2.1.5"
        if _at(rom, 0x1C2, h("3e80e002cd")):
            return "2.0.x - 2.0.17"
        return "2.x"
    if _at(rom, 0x20, h("e9")) and _at(rom, 0x28, h("220d20fcc9")) and _at(rom, 0x30, h("1a22130d20fac9")):
        if _at(rom, 0x153, h("f357af31")):
            return "2020.4.0.1 - 2020.4.0.2"
        if _at(rom, 0x153, h("f35731")):
            return "2020.4.0.4" if _at(rom, 0x100, h("1851")) else "2020.4.0.3"
        if _at(rom, 0x157, h("f3575831")):
            if _at(rom, 0xC7, h("67af6f0ea0efd1c9")):
                return "2020.4.0.5.v1 (retracted)"
            if _at(rom, 0xC6, h("67af6f0ea0efd1c9")):
                return "2020.4.0.5 - 2020.4.0.6"
            if _at(rom, 0xC5, h("67af6f0ea0efc9")):
                return "2020.4.2.0 (interim)" if _at(rom, 0xCC, h("f040e680c8afe0917600f091b728f9c9")) else \
                    "2020.4.1.0 - 2020.4.1.1"
            if _at(rom, 0xC4, h("67af6f0ea0c328")):
                return "2020.4.3.0+" if _at(rom, 0xCC, h("f3e0ffaffbe00fc9")) else "2020.4.2.0"
            if _at(rom, 0x100, h("1855")):
                return "2020.4.0.5.v0 (ZGB)"
        return "2020.4 (version unknown)"
    if _at(rom, 0x150, h("f331ffcfafea")) and _at(rom, 0x158, h("21ffdf0e100600320520fc0d20f9cd")):
        return "1.x - 2.0.x"
    return None


_CC65 = re.compile(rb"\xa0\x01\xb1(.)\xaa\x88\xb1\1\xe6\1\xf0\x05\xe6\1\xf0\x03\x60\xe6\1\xe6(.)\x60", re.S)


def identify(platform: str, data: bytes) -> dict | None:
    """{"tool", "version", "language", "pinned" (whether the pinned compiler is the one), "note"} for the toolchain
    a game's bytes say built it, or None when none of the known ones did (most likely hand-written assembly)."""
    if platform in ("gb", "gbc"):
        version = gbdk_version(data)
        if version is None:
            return None
        pinned = version == "2020.4.3.0+"
        return {"tool": "GBDK", "version": version, "language": "C", "pinned": pinned,
                "note": "" if pinned else f"built with GBDK {version}: the pinned compiler is {PINNED}, so a C match may "
                                          "need that older compiler's code generation (flags, or functions left as "
                                          "assembly)"}
    if platform in ("sms", "gg"):
        if _at(data, 0, bytes.fromhex("f3ed56c38000")) and _at(data, 0x80, bytes.fromhex("31f0df")):
            return {"tool": "GBDK-2020", "version": "", "language": "C", "pinned": True, "note": ""}
        for at in (0x7FE0, 0x3FE0, 0x1FE0):
            if _at(data, at, b"SDSC"):
                return {"tool": "SDSC tag", "version": f"{data[at + 4]:x}.{data[at + 5]:02x}", "language": "",
                        "pinned": False, "author": _sdsc_text(data, at + 0x0A), "name": _sdsc_text(data, at + 0x0C),
                        "note": "homebrew (an SDSC tag): built with devkitSMS, SDCC or an assembler, not told which"}
        return None
    if platform == "nes":
        from . import nes
        from .n64 import RomError

        try:
            h = nes.parse_header(data)
        except RomError:
            return None
        prg = data[h["prg_off"]:h["prg_off"] + h["prg_size"]]
        if (m := _CC65.search(prg)) and m.group(2)[0] == m.group(1)[0] + 1:
            return {"tool": "cc65", "version": "", "language": "C", "pinned": False,
                    "note": f"built with cc65 (its runtime is in the ROM): the pinned compiler is {PINNED}'s SDCC, so "
                            "its C won't match cc65's code; document its routines in assembly instead"}
        if len(prg) >= 0x4000:  # (the last 16 KiB of the PRG is at $C000 on the mappers GBDK-2020 builds for)
            reset = prg[-4] | prg[-3] << 8
            at = len(prg) - 0x4000 + reset - 0xC000
            if reset >= 0xC000 and re.match(rb"\x78\xa2\xff\x9a\xa9\x00\x8d..\x8d\x00\x20\x8d\x01\x20",
                                            prg[at:at + 15], re.S):
                return {"tool": "GBDK-2020", "version": "", "language": "C", "pinned": True, "note": ""}
        return None
    return None


def _sdsc_text(data: bytes, ptr_at: int) -> str:
    """A string an SDSC tag points at (zero-terminated; $FFFF: none)."""
    if ptr_at + 1 >= len(data):
        return ""
    ptr = data[ptr_at] | data[ptr_at + 1] << 8
    if ptr == 0xFFFF or ptr >= len(data):
        return ""
    end = data.find(b"\x00", ptr, ptr + 256)
    return data[ptr:end if end >= 0 else ptr + 64].decode("latin-1", "replace").strip()
