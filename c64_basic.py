"""
c64_basic.py -- parse a Commodore 64 .prg as a BASIC V2 linked list starting at
the load address (default $0801) and decode every token.

The .prg on tape carries a 2-byte load-address *header* before the program body,
so program bytes begin at file offset 2 and map to the load address ($0801).
BASIC V2 storage is a singly-linked list of lines:

    [next_ptr_lo][next_ptr_hi]   little-endian; 0x0000 = end of program
    [line_no_lo][line_no_hi]     little-endian
    [tokenized body...]          tokens (byte >= $80) expand to keywords
    [0x00]                       end-of-line marker

The first line's stored next-pointer is often stale (one byte past the EOL /
on the high byte of the terminating null). We follow each pointer verbatim and
stop on a 0x0000 next-pointer -- we never assume sequential lines (per c64-wiki
and the Grok analysis of MICROCOSM.prg).

Anything past the terminating null is not part of the BASIC list -- the caller
treats that as the machine-code region.

Token reference: https://www.c64-wiki.com/wiki/BASIC_token
"""

from __future__ import annotations

import sys
from pathlib import Path

# Commodore BASIC V2 keyword tokens: 0x80..0xCB (command/byword/operator/function
# classes), plus GO at 0xCB which is an out-of-class command.
BASIC_TOKENS: dict[int, str] = {
    0x80: "END", 0x81: "FOR", 0x82: "NEXT", 0x83: "DATA", 0x84: "INPUT#",
    0x85: "INPUT", 0x86: "DIM", 0x87: "READ", 0x88: "LET", 0x89: "GOTO",
    0x8A: "RUN", 0x8B: "IF", 0x8C: "RESTORE", 0x8D: "GOSUB", 0x8E: "RETURN",
    0x8F: "REM", 0x90: "STOP", 0x91: "ON", 0x92: "WAIT", 0x93: "LOAD",
    0x94: "SAVE", 0x95: "VERIFY", 0x96: "DEF", 0x97: "POKE", 0x98: "PRINT#",
    0x99: "PRINT", 0x9A: "CONT", 0x9B: "LIST", 0x9C: "CLR", 0x9D: "CMD",
    0x9E: "SYS", 0x9F: "OPEN", 0xA0: "CLOSE", 0xA1: "GET", 0xA2: "NEW",
    0xA3: "TAB(", 0xA4: "TO", 0xA5: "FN", 0xA6: "SPC(", 0xA7: "THEN",
    0xA8: "NOT", 0xA9: "STEP", 0xAA: "+", 0xAB: "-", 0xAC: "*", 0xAD: "/",
    0xAE: "^", 0xAF: "AND", 0xB0: "OR", 0xB1: ">", 0xB2: "=", 0xB3: "<",
    0xB4: "SGN", 0xB5: "INT", 0xB6: "ABS", 0xB7: "USR", 0xB8: "FRE",
    0xB9: "POS", 0xBA: "SQR", 0xBB: "RND", 0xBC: "LOG", 0xBD: "EXP",
    0xBE: "COS", 0xBF: "SIN", 0xC0: "TAN", 0xC1: "ATN", 0xC2: "PEEK",
    0xC3: "LEN", 0xC4: "STR$", 0xC5: "VAL", 0xC6: "ASC", 0xC7: "CHR$",
    0xC8: "LEFT$", 0xC9: "RIGHT$", 0xCA: "MID$", 0xCB: "GO",
}

# Keywords that take a decimal target address in their body (loader entry points).
# Only SYS takes a machine-code address (RUN takes a line number, LOAD a filename).
ENTRY_KEYWORDS = frozenset({"SYS"})

HEADER_LEN = 2          # 2-byte load-address header before the program body
LOAD_ADDR = 0x0801      # default load address = first program byte


def read_u16le(data: bytes, off: int) -> int:
    return data[off] | (data[off + 1] << 8)


def decode_body(body: bytes) -> str:
    """Expand BASIC tokens to [KEYWORD] placeholders; leave readable ASCII as-is
    and render non-printables as .xx so the decoded line stays monospace."""
    out: list[str] = []
    for b in body:
        if b >= 0x80:
            kw = BASIC_TOKENS.get(b, f"<TOKEN ${b:02x}>")
            out.append(f"[{kw}]")
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f".{b:02x}")
    return "".join(out)


def entry_targets(body: bytes) -> list[tuple[int, int]]:
    """Return (address, byte_offset) of every decimal literal following an entry
    keyword (SYS/RUN/LOAD) in this line's body. These are the machine-code entry
    points the loader hands off to."""
    targets: list[tuple[int, int]] = []
    i = 0
    n = len(body)
    while i < n:
        tok = BASIC_TOKENS.get(body[i])
        if tok and tok in ENTRY_KEYWORDS and i + 1 < n:
            j = i + 1
            while j < n and body[j] in (0x20, 0x28):  # SYS 2061 / SYS(2061)
                j += 1
            start = j
            digits = []
            while j < n and 48 <= body[j] <= 57:  # ASCII 0-9
                digits.append(chr(body[j]))
                j += 1
            if digits:
                targets.append((int("".join(digits)), start))
        i += 1
    return targets


def parse_basic(data: bytes, load_addr: int = LOAD_ADDR) -> dict:
    """Parse the BASIC linked list out of `data` (program bytes, header already
    stripped, so data[0] sits at `load_addr`). Returns a summary dict: lines,
    BASIC-end offset/address (offsets are into `data`) and the machine-code region.

    The interpreter relinks lines after LOAD, so stored next-pointers can be
    stale: we walk physically (the next line starts right after the 0 that ends
    this one) and record a mismatching stored pointer as `stale_ptr`."""
    lines: list[dict] = []
    off = 0
    seen: set[int] = set()
    end_off = None
    while off + 2 <= len(data) and off not in seen:
        seen.add(off)
        next_ptr = read_u16le(data, off)
        if next_ptr == 0:
            end_off = off + 2  # skip the 2-byte null next-pointer
            break
        if off + 4 > len(data):
            break
        line_no = read_u16le(data, off + 2)
        i = off + 4
        while i < len(data) and data[i] != 0:
            i += 1
        if i >= len(data):
            break  # unterminated line: not BASIC
        body = data[off + 4:i]
        line = {
            "addr": load_addr + off,
            "end_addr": load_addr + i,
            "line_no": line_no,
            "body": body,
            "hex": body.hex(" "),
            "decoded": decode_body(body),
            "entry_targets": entry_targets(body),
        }
        if next_ptr != load_addr + i + 1:
            line["stale_ptr"] = next_ptr
        lines.append(line)
        off = i + 1
    if end_off is None:
        end_off = off
    return {
        "lines": lines,
        "basic_end_off": end_off,
        "basic_end_addr": load_addr + end_off,
        "machine_code_off": end_off,
        "machine_code_addr": load_addr + end_off,
    }


def _fmt_line(l: dict) -> str:
    tgt = ", ".join(f"${t[0]:04X}" for t in l["entry_targets"])
    tgt_s = f"   entry: {tgt}" if tgt else ""
    return (f"${l['addr']:04X}-{l['end_addr']:04X}  "
            f"line {l['line_no']:>5}  {l['decoded']}{tgt_s}")


def main(paths: list[str]) -> int:
    if not paths:
        print("usage: python c64_basic.py <file.prg> [<file.prg> ...]")
        return 2
    for path in paths:
        p = Path(path)
        raw = p.read_bytes()
        if len(raw) < HEADER_LEN + 2:
            print(f"{p}: too small ({len(raw)} bytes) to be a .prg")
            continue
        load_addr = raw[0] | (raw[1] << 8)
        data = raw[HEADER_LEN:]  # program body, header stripped
        print("=" * 72)
        print(f"{p.name}: {len(raw)} bytes total, load addr ${load_addr:04X}, "
              f"body {len(data)} bytes @ file offset {HEADER_LEN}")
        print("-" * 72)
        res = parse_basic(data, load_addr)
        if not res["lines"]:
            print("  (no BASIC lines found -- machine-code only)")
        else:
            for l in res["lines"]:
                print("  " + _fmt_line(l))
        print("-" * 72)
        print(f"  BASIC occupies file offset {HEADER_LEN}..{HEADER_LEN + res['basic_end_off']} "
              f"(${load_addr:04X}-${res['basic_end_addr']:04X})")
        mc_bytes = len(data) - res["machine_code_off"]
        print(f"  MACHINE CODE starts at ${res['machine_code_addr']:04X} "
              f"(file offset {HEADER_LEN + res['machine_code_off']}); "
              f"{mc_bytes} bytes remain to end of .prg")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
