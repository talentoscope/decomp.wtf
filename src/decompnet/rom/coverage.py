"""What an emulator saw a game do while someone played it: which ROM bytes ran as code, which it read as data, and where
functions were entered. Static following can't see code reached through pointers kept in RAM, or tell a table from
code nothing reaches; a playthrough can, for everything it went through. Every finished disassembly of these consoles
leaned on one.

Two forms are read:

- a Mesen2 code/data log (`.cdl`): "CDLv2", the CRC32 of the ROM it was made for, then one flag byte per ROM byte:
  0x01 ran as code, 0x02 read as data, 0x04 a jump went there, 0x08 a call or an interrupt entered there. A log
  without the header (older files) is one flag byte per ROM byte. Mesen2 writes them for the Game Boy, the Master
  System and the NES alike, indexed by where the byte is in the ROM (so its bank is known); FCEUX's NES logs are
  one byte per byte of program and character ROM, code and data in the same two bits;
- an execution trace: one `bank:address` per instruction that ran (hex), as recompilers and emulator scripts write
  them. Calls and `rst`s that ran give where functions were entered.

Absence from a log never means data: a playthrough doesn't go everywhere.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

CODE, DATA, JUMP, ENTRY = 0x01, 0x02, 0x04, 0x08
_TRACE = re.compile(r"^\s*(?:0x|\$)?([0-9A-Fa-f]{1,3})\s*:\s*(?:0x|\$)?([0-9A-Fa-f]{4})\b")


class CoverageError(ValueError):
    pass


@dataclass
class Coverage:
    """Per ROM byte (by its offset in the ROM): `code` it ran as code, `read` it was read as data (and never ran);
    `entries`: where calls or interrupts entered. `kind`: cdl | trace."""

    code: bytearray
    read: bytearray
    entries: set[int] = field(default_factory=set)
    kind: str = "cdl"

    def summary(self) -> dict:
        return {"kind": self.kind, "code": sum(self.code), "read": sum(self.read), "entries": len(self.entries)}

    def runs(self, which: str = "read", least: int = 2) -> list[tuple[int, int]]:
        """(offset, size) of each run of `which` bytes at least `least` long."""
        flags, out, k = getattr(self, which), [], 0
        while k < len(flags):
            if flags[k]:
                end = k
                while end < len(flags) and flags[end]:
                    end += 1
                if end - k >= least:
                    out.append((k, end - k))
                k = end
            else:
                k += 1
        return out


def read_cdl(raw: bytes, rom: bytes, crcs: set[int] | None = None, fceux: int | None = None) -> Coverage:
    """A code/data log for `rom` (the bytes it indexes: the whole ROM on the Game Boy and the Master System, the
    program ROM on the NES). Refused when it's for another version of the game. `crcs`: the CRC32s a Mesen2 log for
    this game may carry (it takes it over the game as it loaded it: by default `rom`'s). `fceux`: the size of an FCEUX
    log for this game (NES: program and character ROM, the program's first; its bits above data say other things,
    so it gives no entries)."""
    plain = False
    if raw[:5] == b"CDLv2":
        crc, flags = int.from_bytes(raw[5:9], "little"), raw[9:]
        if len(flags) != len(rom):
            raise CoverageError(f"this log covers {len(flags):,} bytes, the game has {len(rom):,}: it's for another game")
        if crc not in (crcs or {zlib.crc32(rom)}):
            raise CoverageError("this log is for another version of the game (its CRC32 differs)")
    elif len(raw) == len(rom) or (fceux and len(raw) == fceux):
        flags, plain = raw[:len(rom)], bool(fceux)
    else:
        raise CoverageError("not a code/data log: one starts with CDLv2, or has one byte per byte of the game")
    code = bytearray(1 if f & CODE else 0 for f in flags)
    read = bytearray(1 if f & DATA and not f & CODE else 0 for f in flags)
    entries = set() if plain else {k for k, f in enumerate(flags) if f & ENTRY and f & CODE}
    if not any(code):
        raise CoverageError("this log has no code in it: play the game with the code/data logger on, then save it")
    return Coverage(code, read, entries, "cdl")


def read_trace(text: str, rom: bytes, flat_of, decode) -> Coverage:
    """An execution trace (`bank:address` per instruction that ran). `flat_of(bank, cpu)` gives a ROM offset (None:
    not ROM); `decode(flat)` gives the instruction there as (text, length, kind, target), kind as the lanes' `flow`."""
    code, read = bytearray(len(rom)), bytearray(len(rom))
    ran: dict[int, set[int]] = {}  # CPU address -> the ROM offsets it ran from (banks share addresses)
    seen = 0
    for line in text.splitlines():
        m = _TRACE.match(line)
        if not m:
            continue
        flat = flat_of(int(m.group(1), 16), int(m.group(2), 16))
        if flat is None or not 0 <= flat < len(rom):
            continue
        seen += 1
        if code[flat]:
            continue
        _text, ln, _kind, _target = decode(flat)
        code[flat:flat + ln] = b"\x01" * min(ln, len(rom) - flat)
        ran.setdefault(int(m.group(2), 16), set()).add(flat)
    if not seen:
        raise CoverageError("no `bank:address` lines in that file: is it an execution trace?")
    entries = set()
    for flats in list(ran.values()):
        for flat in flats:
            _text, _ln, kind, target = decode(flat)
            if kind == "call" and target is not None and len(ran.get(target, ())) == 1:
                entries |= ran[target]  # a call that ran, to an address that ran in one bank only
    return Coverage(code, read, entries, "trace")
