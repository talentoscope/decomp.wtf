"""Commodore BASIC V2: walk a program's line list and pull out SYS targets.

The line link is a convenience, not a source of truth. A packer or a bad save leaves the next-line
pointer stale (it can land in the middle of the following marker, or outside the file). This walker
trusts a pointer only when it moves strictly forward, stays inside the buffer and looks like a line.
Otherwise it continues at the byte after the line's terminating zero. A 0x00 0x00 link ends the
program. Cycles and pointers that run backwards stop the walk.

Only the SYS token (0x9E) yields a machine-code address. Spaces and one optional opening parenthesis
may sit between the token and the number (``SYS 2061``, ``SYS(2061)``). RUN's number is a line number
and LOAD's operand is a filename; neither is an address. SYS inside quotes, or in the raw text after
REM, is ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

# 0x80-0xCB. 0x9E is SYS (not OPEN); 0x9F is OPEN; 0xAE is the power operator; 0xFF is pi.
TOKENS = {
    0x80: "END", 0x81: "FOR", 0x82: "NEXT", 0x83: "DATA", 0x84: "INPUT#", 0x85: "INPUT",
    0x86: "DIM", 0x87: "READ", 0x88: "LET", 0x89: "GOTO", 0x8A: "RUN", 0x8B: "IF",
    0x8C: "RESTORE", 0x8D: "GOSUB", 0x8E: "RETURN", 0x8F: "REM", 0x90: "STOP", 0x91: "ON",
    0x92: "WAIT", 0x93: "LOAD", 0x94: "SAVE", 0x95: "VERIFY", 0x96: "DEF", 0x97: "POKE",
    0x98: "PRINT#", 0x99: "PRINT", 0x9A: "CONT", 0x9B: "LIST", 0x9C: "CLR", 0x9D: "CMD",
    0x9E: "SYS", 0x9F: "OPEN", 0xA0: "CLOSE", 0xA1: "GET", 0xA2: "NEW", 0xA3: "TAB(",
    0xA4: "TO", 0xA5: "FN", 0xA6: "SPC(", 0xA7: "THEN", 0xA8: "NOT", 0xA9: "STEP",
    0xAA: "+", 0xAB: "-", 0xAC: "*", 0xAD: "/", 0xAE: "^", 0xAF: "AND", 0xB0: "OR",
    0xB1: ">", 0xB2: "=", 0xB3: "<", 0xB4: "SGN", 0xB5: "INT", 0xB6: "ABS", 0xB7: "USR",
    0xB8: "FRE", 0xB9: "POS", 0xBA: "SQR", 0xBB: "RND", 0xBC: "LOG", 0xBD: "EXP",
    0xBE: "COS", 0xBF: "SIN", 0xC0: "TAN", 0xC1: "ATN", 0xC2: "PEEK", 0xC3: "LEN",
    0xC4: "STR$", 0xC5: "VAL", 0xC6: "ASC", 0xC7: "CHR$", 0xC8: "LEFT$", 0xC9: "RIGHT$",
    0xCA: "MID$", 0xCB: "GO", 0xFF: "π",
}
SYS = 0x9E
REM = 0x8F
MAX_LINE = 63999


@dataclass
class BasicLine:
    number: int
    text: str
    address: int  # CPU address of this line's link word


@dataclass
class BasicProgram:
    lines: list[BasicLine]
    sys: list[int]          # every SYS address, in order
    end: int                # CPU address just past the end marker (or the load address if there was nothing)
    text: str

    @property
    def entry(self) -> int | None:
        """The first SYS target, the only BASIC statement that starts machine code."""
        return self.sys[0] if self.sys else None


def _sys_number(payload: bytes, i: int, limit: int) -> int | None:
    """Digits after a SYS token, skipping spaces and one optional '('."""
    while i < limit and payload[i] == 0x20:
        i += 1
    if i < limit and payload[i] == 0x28:  # one optional '('
        i += 1
        while i < limit and payload[i] == 0x20:
            i += 1
    start = i
    while i < limit and 0x30 <= payload[i] <= 0x39:
        i += 1
    if i == start:
        return None
    value = int(payload[start:i])
    return value if value <= 0xFFFF else None


def _detokenise(payload: bytes, start: int, end: int) -> tuple[str, list[int]]:
    """Plain text of one line, and the SYS addresses it contains. ``end`` is the line's terminating zero."""
    out: list[str] = []
    sys: list[int] = []
    i, quote, rem = start, False, False
    while i < end:
        b = payload[i]
        if rem:
            out.append(chr(b) if 32 <= b < 127 else f"{{{b:02X}}}")
            i += 1
            continue
        if b == 0x22:  # quote
            quote = not quote
            out.append('"')
            i += 1
            continue
        if quote:
            out.append(chr(b) if 32 <= b < 127 else f"{{{b:02X}}}")
            i += 1
            continue
        if b == REM:
            rem = True
            out.append("REM")
            i += 1
            continue
        if b == SYS:
            out.append("SYS")
            n = _sys_number(payload, i + 1, end)
            if n is not None:
                sys.append(n)
            i += 1
            continue
        name = TOKENS.get(b)
        if name is not None:
            out.append(name)
            if name[0].isalpha() and i + 1 < end and (payload[i + 1] in (0x28, 0x22) or 48 <= payload[i + 1] < 127):
                out.append(" ")
            i += 1
            continue
        out.append(chr(b) if 32 <= b < 127 else f"{{{b:02X}}}")
        i += 1
    return "".join(out), sys


def _line_pointer(payload: bytes, off: int, load: int) -> int | None:
    """Where the link at ``off`` goes, if that place is the end marker or another line further on.

    A stale link (the sample pattern: the pointer lands on the high byte of the end marker and the
    next word reads as an address past the buffer) is rejected. The caller then steps to the byte
    after the line instead."""
    if off < 0 or off + 2 > len(payload):
        return None
    if payload[off] == 0 and payload[off + 1] == 0:
        return off
    if off + 4 > len(payload):
        return None
    number = payload[off + 2] | (payload[off + 3] << 8)
    link = payload[off] | (payload[off + 1] << 8)
    if number > MAX_LINE:
        return None
    if link == 0:
        return off
    nxt = link - load
    if off < nxt <= len(payload):
        return off
    return None


def decode(payload: bytes, load: int) -> BasicProgram:
    """Walk the program stored at ``load``. ``payload`` is the bytes from that address on, with no PRG header."""
    lines: list[BasicLine] = []
    sys: list[int] = []
    off, seen = 0, set()
    end = load
    while off + 1 < len(payload):
        if off in seen or off < 0:
            break
        seen.add(off)
        if payload[off] == 0 and payload[off + 1] == 0:
            end = load + off + 2
            break
        if off + 4 > len(payload):
            end = load + len(payload)
            break
        number = payload[off + 2] | (payload[off + 3] << 8)
        # The line ends at its zero byte. Don't scan past a second zero (that would eat the end marker).
        text_at = off + 4
        eol = text_at
        while eol < len(payload) and payload[eol] != 0:
            eol += 1
        if eol >= len(payload):
            end = load + len(payload)
            break
        text, found = _detokenise(payload, text_at, eol)
        if number <= MAX_LINE:
            lines.append(BasicLine(number, text, load + off))
            sys.extend(found)
        after = eol + 1
        link = payload[off] | (payload[off + 1] << 8)
        ptr = link - load
        # Follow the link only when it moves strictly past this line and lands on a real line (or the
        # end marker). Anything else, including a pointer one byte into the end marker, is ignored.
        if link and after < ptr < len(payload) and _line_pointer(payload, ptr, load) is not None:
            off = ptr
        else:
            off = after
        end = load + off
    else:
        end = load + off
    text = "\n".join(f"{ln.number} {ln.text}".rstrip() for ln in lines)
    return BasicProgram(lines, sys, end, text)
