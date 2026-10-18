"""Judging a hand-written Game Boy routine an agent documented: the assembly track.

Most Game Boy games were written in assembly, so for most of a game there is no C to match: what decompiling it means
is assembly that rebuilds it byte for byte, with names, comments and constants instead of addresses. An agent's
routine (RGBDS syntax) is accepted when all of these hold:

1. **Byte-identical.** Assembled at the routine's address in its bank (rgbasm and rgblink, with hardware.inc 6.0, its
   compat file and the project's symbols included), it's the ROM's bytes there, no more and no fewer.
2. **It survives a shift test.** Assembled again with the routine and every address the project knows moved, each
   operand that is an address (a call or jump target, a memory operand, a 16-bit constant in ROM or RAM) changes: a
   raw address left in doesn't. A 16-bit constant that only looks like an address is marked `; number`.
3. **Names from the right bank.** A call, jump or pointer into the switchable bank names a function in the bank that's
   mapped there: the routine's own, or (from bank 0) the one the split followed it into. Where no one knows which, it
   waits until someone does (a correction, a coverage log) rather than accept a guess.
4. **Instructions stay instructions.** `db` and `dw` only where the map has data in the routine: a routine's operands
   after a call, a table, an instruction jumped into the middle of.
5. **Hardware by name.** Assembled once more with hardware.inc's registers and areas moved, each operand that is one
   (an I/O register, a mapper register, video memory) changes: `ldh [rLCDC], a`, not `ldh [$ff40], a`.
6. **Real names and a header comment.** The routine's label and its local labels aren't automatic ones (`Call_002_4a10`,
   `.jr_4a1c`), and comment lines before it say what it does (with the banks it maps, when it maps one).

`DEF NAME EQU value` lines name addresses (and numbers) for the project: they move with the shift test like the
project's own, and an accepted routine's become project symbols. What reads or writes files, defines macros or
changes how the rest assembles (INCLUDE, INCBIN, SECTION, MACRO, REPT, EQUS, OPT...) is refused: agents' text is
assembled on the user's computer.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import gb_hw, sm83

BANK = 0x4000
MAX_TEXT = 64 * 1024
SHIFT = 0x10
_IDENT = r"[A-Za-z_][\w@#$]*"
_LABEL = re.compile(rf"^\s*(\.?{_IDENT}(?:\.[\w@#$]+)?)(::?)\s*")
_DEFINE = re.compile(rf"^\s*(?:def\s+({_IDENT})\s+equ\b|({_IDENT})\s+equ\b)\s*(.+)$", re.I)
_BANNED = re.compile(
    r"^(?:include|incbin|section|macro|endm|rept|endr|for|shift|if|elif|else|endc|print|println|warn|fail|assert|"
    r"static_assert|export|purge|pushs|pops|pushc|popc|pusho|popo|opt|load|endl|union|nextu|endu|charmap|newcharmap|"
    r"setcharmap|rsreset|rsset|rb|rw|ds|redef|align|break|endsection|fragment|newcharmap)\b", re.I)  # (not rl: an instruction)
# automatic names: the split's, mgbdis's, or anything that only spells an address
AUTO = re.compile(r"^\.?(?:(?:Call|Func|Jump|jr|func|sub|loc|label|lbl|L|unk|unknown)_?[0-9A-Fa-f_]*|.*_[0-9A-Fa-f]{4})$")


class AsmError(ValueError):
    pass


@dataclass
class Routine:
    """What the judge needs to know about the function an agent documented."""

    name: str
    bank: int
    cpu: int
    code: bytes                      # the ROM's bytes for it
    symbols: dict[str, int]          # the project's names -> addresses (functions, data)
    functions: dict[int, str] = field(default_factory=dict)   # ROM offset -> name, every function of the project
    data: list[tuple[int, int, str]] = field(default_factory=list)  # (ROM offset, size, kind): data regions in it
    edges: dict[int, int] = field(default_factory=dict)       # offset in the routine -> ROM offset its call/jump goes to
    mapper: str = "mbc5"

    @property
    def flat(self) -> int:
        return self.cpu if self.bank == 0 else self.bank * BANK + self.cpu - BANK


@dataclass
class Verdict:
    ok: bool
    same: int                        # bytes identical to the ROM's, from the start
    size: int                        # bytes the text assembled to
    problems: list[str]              # what fails, numbered by check
    label: str = ""                  # the routine's own label (its name, once accepted)
    defines: dict[str, int] = field(default_factory=dict)  # DEF lines: name -> value
    text: str = ""                   # the text as kept (without its DEF lines, which become project symbols)


@dataclass
class _Item:
    line: int
    kind: str                        # insn | db | dw | def
    text: str                        # the statement, without its labels or comment
    comment: str
    size: int = 0                    # db/dw: bytes


# ---------------------------------------------------------------- reading the text

def _split_comment(line: str) -> tuple[str, str]:
    out, quote = [], None
    for k, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ";":
            return "".join(out), line[k + 1:]
        out.append(ch)
    return "".join(out), ""


def _data_size(kind: str, args: str) -> int:
    size, item, quote = 0, "", None
    for ch in args + ",":
        if quote:
            item += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            item += ch
        elif ch == ",":
            item = item.strip()
            if item.startswith('"') and item.endswith('"') and kind == "db":
                size += len(item) - 2
            elif item:
                size += 1 if kind == "db" else 2
            item = ""
        else:
            item += ch
    return size


def contained(text: str) -> bool:
    """Whether text is only what the judge takes: no INCLUDE, INCBIN, macros or other directives (a room member checks
    what its host sends before keeping it)."""
    try:
        return not any("isn't taken here" in p for p in parse(text)[3])
    except AsmError:
        return False


def parse(text: str) -> tuple[list[_Item], list[tuple[int, str]], list[str], list[str]]:
    """(items, labels as (line, name), the comment lines before the first label, problems): one statement a line."""
    if len(text) > MAX_TEXT:
        raise AsmError(f"the routine is over {MAX_TEXT // 1024} KB of text")
    items, labels, header, problems = [], [], [], []
    for n, raw in enumerate(text.splitlines(), 1):
        code, comment = _split_comment(raw)
        if not code.strip():
            if comment.strip() and not labels:
                header.append(comment.strip())
            continue
        while (m := _LABEL.match(code)):
            labels.append((n, m.group(1)))
            code = code[m.end():]
        stmt = code.strip()
        if not stmt:
            continue
        if re.search(r"\bequs\b|[{}\\]", stmt, re.I) or _BANNED.match(stmt):
            problems.append(f"line {n}: `{stmt.split()[0]}` isn't taken here: instructions, labels, db/dw and DEF "
                            "NAME EQU value only")
            continue
        if (m := _DEFINE.match(stmt)):
            items.append(_Item(n, "def", stmt, comment))
            continue
        op = stmt.split(None, 1)[0].lower()
        if op in ("db", "dw"):
            items.append(_Item(n, op, stmt, comment, _data_size(op, stmt[2:])))
        else:
            items.append(_Item(n, "insn", stmt, comment))
    if not any(i.kind != "def" for i in items):
        problems.append("there's no code in it")
    return items, labels, header, problems


# ---------------------------------------------------------------- assembling

def _hardware_names() -> set[str]:
    names = set()
    for f in ("hardware.inc", "hardware_compat.inc"):
        for line in (gb_hw.include_dir() / f).read_text(encoding="utf-8").splitlines():
            if (m := re.match(r"^\s*def\s+([A-Za-z_]\w*)\s+equ\b", line, re.I)):
                names.add(m.group(1))
    return names | set(gb_hw.old_names())


_HW = None


def hardware_names() -> set[str]:
    global _HW
    if _HW is None:
        _HW = _hardware_names()
    return _HW


def _moved(value: int, delta: int) -> int:
    """An address the shift test moves: ROM and RAM ones by `delta`, HRAM ones by 16 kept in HRAM; not numbers, not
    hardware."""
    if 0x100 <= value < 0x8000 or 0xA000 <= value < 0xFE00:
        return (value + delta) & 0xFFFF
    if 0xFF80 <= value < 0xFFFF:
        return value + SHIFT if value + SHIFT < 0xFFFF else value - SHIFT
    return value


_MAPPER_NAMES = {"rRAMG", "rROMB", "rROMB0", "rROMB1", "rRAMB", "rBMODE", "rRTCLATCH", "rRTCREG"}


def _hardware_moved(text: str) -> str:
    """hardware.inc (or its compat file) with its registers, the mapper's registers and the memory areas moved by one."""
    def bump(m: re.Match) -> str:
        name, value = m.group(2), int(m.group(3), 16)
        if 0x8000 <= value <= 0xFFFF or name in _MAPPER_NAMES:
            value = value - 1 if value == 0xFFFF else value + 1
        return f"{m.group(1)}{value:04X}"

    return re.sub(r"(?im)^(\s*def\s+([A-Za-z_]\w*)\s+equ\s+\$)([0-9A-F]{4})\b", bump, text)


def _defs_moved(items: list[_Item], lines: list[str], delta: int) -> list[str]:
    """The text's DEF lines with their value moved like an address of the project's (when it is one)."""
    out = list(lines)
    for it in items:
        if it.kind != "def":
            continue
        m = _DEFINE.match(it.text)
        name, e = m.group(1) or m.group(2), f"({m.group(3)})"
        rom = f"(({e} >= $100 && {e} < $8000) || ({e} >= $A000 && {e} < $FE00))"
        up = f"({e} >= $FF80 && {e} < $FFFF - {SHIFT})"
        down = f"({e} >= $FFFF - {SHIFT} && {e} < $FFFF)"
        out[it.line - 1] = f"def {name} equ {e} + {rom} * ({delta}) + {up} * {SHIFT} - {down} * {SHIFT}"
    return out


def _tools():
    from . import gb_mgbdis

    ts = gb_mgbdis.tools()
    if ts is None:
        raise AsmError("RGBDS isn't installed (Setup lists it)")
    return ts


def _assemble(tmp: Path, r: Routine, lines: list[str], symbols: dict[str, int], at: int, hw_moved: bool) -> bytes:
    """The text assembled at CPU address `at` in the routine's bank: the bytes of its section."""
    from .gb_build import _run

    ts = _tools()
    for f in ("hardware.inc", "hardware_compat.inc"):
        text = (gb_hw.include_dir() / f).read_text(encoding="utf-8")
        (tmp / f).write_text(_hardware_moved(text) if hw_moved else text, encoding="utf-8")
    (tmp / "old_names.inc").write_text(gb_hw.old_names_inc(), encoding="utf-8")
    (tmp / "project.inc").write_text("".join(f"def {n} equ ${v & 0xFFFF:04X}\n" for n, v in sorted(symbols.items())),
                                     encoding="utf-8")
    where = f"ROM0[${at:04X}]" if r.bank == 0 else f"ROMX[${at:04X}], BANK[${r.bank:X}]"
    (tmp / "routine.asm").write_text(
        'INCLUDE "hardware.inc"\nINCLUDE "hardware_compat.inc"\nINCLUDE "old_names.inc"\nINCLUDE "project.inc"\n'
        f'SECTION "routine", {where}\n' + "\n".join(lines) + "\n", encoding="utf-8")
    rc, log = _run([ts.rgbasm, "-o", "routine.o", "routine.asm"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset=5))
    rc, log = _run([ts.rgblink, "-o", "out.gb", "-m", "out.map", "routine.o"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset=5))
    size = 0
    for line in (tmp / "out.map").read_text(encoding="utf-8", errors="replace").splitlines():
        if (m := re.search(r"\$([0-9A-Fa-f]{4})-\$([0-9A-Fa-f]{4}) \(\$([0-9A-Fa-f]+) bytes?\) \[\"routine\"\]", line)):
            size = int(m.group(3), 16)
        elif '["routine"]' in line and "empty" in line.lower():
            size = 0
    flat = at if r.bank == 0 else r.bank * BANK + at - BANK
    return (tmp / "out.gb").read_bytes()[flat:flat + size]


def _clean(log: str, offset: int) -> str:
    """An assembler's message for the agent: its own file's line numbers (the wrapper adds `offset` lines before
    the text), no paths."""
    out = []
    for line in log.splitlines():
        line = re.sub(r"\S*routine\.asm\((\d+)\)", lambda m: f"line {int(m.group(1)) - offset}", line)
        line = re.sub(r"\S*/(\w+\.(?:inc|asm|o))", r"\1", line)
        if line.strip():
            out.append(line.strip())
    text = "\n".join(out[-8:]) or "the assembler refused it"
    if (m := re.search(r"Undefined symbol `([^`]+)`", text)):
        text += (f"\n({m.group(1)} isn't a name the project has: use one it has (the brief's draft does), or name the "
                 f"address yourself with DEF {m.group(1)} EQU $XXXX)")
    return text


# ---------------------------------------------------------------- the checks

def _operands(text: str, ln: int):
    """(role, value, byte positions) for an instruction's address-like operand, or None. Roles: code (an absolute
    call or jump target), read / write (a memory operand), hmem (ldh), imm16 (a 16-bit constant)."""
    op = text.split(" ", 1)[0]
    if ln == 3 and op in ("call", "jp") and (m := re.search(r"\$([0-9a-f]{4})$", text)):
        return "code", int(m.group(1), 16), (1, 2)
    if (m := re.match(r"^ld \(\$([0-9a-f]{4})\), (a|sp)$", text)):
        return "write", int(m.group(1), 16), (1, 2)
    if (m := re.match(r"^ld a, \(\$([0-9a-f]{4})\)$", text)):
        return "read", int(m.group(1), 16), (1, 2)
    if (m := re.match(r"^ldh (?:\(\$ff00\+\$([0-9a-f]{2})\), a|a, \(\$ff00\+\$([0-9a-f]{2})\))$", text)):
        return "hmem", 0xFF00 + int(m.group(1) or m.group(2), 16), (1,)
    if (m := re.match(r"^ld (?:bc|de|hl|sp), \$([0-9a-f]{4})$", text)):
        return "imm16", int(m.group(1), 16), (1, 2)
    return None


def _hardware(role: str, value: int) -> bool:
    """Whether an operand names hardware (checked by moving hardware.inc), not an address of the game's."""
    if 0x8000 <= value < 0xA000 or 0xFE00 <= value < 0xFF80 or value == 0xFFFF:
        return True  # video memory, OAM, I/O
    return role == "write" and value < 0x8000  # a write to ROM: a mapper register


def judge(r: Routine, text: str, tmp_root: Path | None = None) -> Verdict:
    """Whether `text` documents the routine (see the module's six checks)."""
    items, labels, header, problems = parse(text)
    globals_ = [name for _n, name in labels if not name.startswith(".") and "." not in name]
    label = globals_[0] if globals_ else ""
    if problems:
        return Verdict(False, 0, 0, problems, label)
    if not label:
        return Verdict(False, 0, 0, ["give the routine its label (its name) before its first instruction"], label)
    defines: dict[str, int] = {}
    own = {name for _n, name in labels} | {(_DEFINE.match(i.text).group(1) or _DEFINE.match(i.text).group(2))
                                           for i in items if i.kind == "def"}
    hw = hardware_names()
    # a label that's already the project's name for something else would be two things at once in the exported build
    taken = [n for n in globals_ if n in r.symbols and n != r.name and not r.cpu <= r.symbols[n] < r.cpu + len(r.code)]
    if taken:
        return Verdict(False, 0, 0, [f"6. {', '.join(taken)} already name{'s' if len(taken) == 1 else ''} something else "
                                     "in the project: pick another name"], label)
    if (clash := sorted(own & hw)):
        return Verdict(False, 0, 0, [f"{', '.join(clash)} already name{'s' if len(clash) == 1 else ''} hardware "
                                     "(hardware.inc): pick another name"], label)
    symbols = {n: v for n, v in r.symbols.items() if n not in own and n not in hw and re.fullmatch(_IDENT, n)}
    lines = text.splitlines()
    with tempfile.TemporaryDirectory(prefix="gb-asm-", dir=tmp_root) as tmp:
        tmp = Path(tmp)
        try:
            got = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=False)
        except AsmError as e:
            return Verdict(False, 0, 0, [f"1. it doesn't assemble: {e}"], label)
        same = next((k for k, (a, b) in enumerate(zip(got, r.code)) if a != b), min(len(got), len(r.code)))
        if got != r.code:
            return Verdict(False, same, len(got), [_difference(r, got, same)], label)
        # what each statement is, against the bytes
        layout, off = [], 0
        for it in items:
            if it.kind == "def":
                continue
            if it.kind == "insn":
                t, ln = sm83.decode(r.code[off:off + 3], r.cpu + off)
                layout.append((it, off, t, ln))
                off += ln
            else:
                layout.append((it, off, it.kind, it.size))
                off += it.size
        if off != len(r.code):
            return Verdict(False, len(r.code), len(got), [
                "4. its lines don't line up with the bytes one statement a line: write one instruction a line, db and "
                "dw for data"], label)
        for it in items:  # the names it defines, as assembled
            if it.kind == "def":
                m = _DEFINE.match(it.text)
                defines[m.group(1) or m.group(2)] = 0
        if defines:
            try:
                defines = _values(tmp, r, lines, symbols, defines)
            except AsmError as e:
                return Verdict(False, len(r.code), len(got), [f"2. {e}"], label)
            clash = [n for n, v in defines.items() if n in r.symbols and r.symbols[n] != v]
            if clash:
                return Verdict(False, len(r.code), len(got), [
                    f"2. {', '.join(clash)} already name{'s' if len(clash) == 1 else ''} another address in the project"],
                    label)
        delta = SHIFT if r.cpu + len(r.code) + SHIFT <= (0x4000 if r.bank == 0 else 0x8000) else -SHIFT
        moved_syms = {n: _moved(v, delta) for n, v in symbols.items()}
        try:
            shifted = _assemble(tmp, r, _defs_moved(items, lines, delta), moved_syms, r.cpu + delta, hw_moved=False)
            hw_shifted = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=True)
        except AsmError as e:
            return Verdict(False, len(r.code), len(got), [f"2. it doesn't assemble moved (the shift test): {e}"], label)
    problems += _shift_checks(r, layout, shifted, hw_shifted)
    problems += _bank_checks(r, layout, symbols)
    problems += _data_checks(r, layout)
    problems += _name_checks(r, labels, header, layout)
    kept = "\n".join(line for k, line in enumerate(lines, 1)
                     if not any(i.kind == "def" and i.line == k for i in items))
    return Verdict(not problems, len(r.code), len(got), sorted(problems), label, defines, kept.strip("\n") + "\n")


def _values(tmp: Path, r: Routine, lines: list[str], symbols: dict[str, int], defines: dict[str, int]) -> dict[str, int]:
    """What the text's DEF names come to (read back from a symbol file of the assembled routine)."""
    from .gb_build import _run

    ts = _tools()
    extra = [f'def __DECOMP_{k} equ {name}' for k, name in enumerate(defines)]
    (tmp / "values.asm").write_text(
        'INCLUDE "hardware.inc"\nINCLUDE "hardware_compat.inc"\nINCLUDE "old_names.inc"\nINCLUDE "project.inc"\n'
        + "\n".join(line for line in lines if _DEFINE.match(_split_comment(line)[0].strip()) or not line.strip())
        + "\n" + "\n".join(extra) + '\nSECTION "values", ROM0\n' + "".join(f"db LOW(__DECOMP_{k}), HIGH(__DECOMP_{k})\n"
                                                                           for k in range(len(defines))),
        encoding="utf-8")
    if _run([ts.rgbasm, "-o", "values.o", "values.asm"], tmp, timeout=30)[0] != 0 or \
            _run([ts.rgblink, "-o", "values.gb", "values.o"], tmp, timeout=30)[0] != 0:
        raise AsmError("a DEF line's value must be a constant: numbers and names, not the routine's labels")
    raw = (tmp / "values.gb").read_bytes()
    return {name: raw[2 * k] | raw[2 * k + 1] << 8 for k, name in enumerate(defines)}


def _difference(r: Routine, got: bytes, at: int) -> str:
    if len(got) != len(r.code) and at >= min(len(got), len(r.code)):
        return (f"1. it assembles to {len(got)} bytes, the routine is {len(r.code)}: "
                + ("it stops early" if len(got) < len(r.code) else "it runs past the routine's end"))
    start = 0
    for addr, _text, ln in sm83.disassemble(r.code, r.cpu):  # the instruction the first difference is in
        if addr - r.cpu + ln > at:
            start = addr - r.cpu
            break
    want, wl = sm83.decode(r.code[start:start + 3], r.cpu + start)
    have, hl = sm83.decode(got[start:start + 3], r.cpu + start)
    return (f"1. the bytes differ at ${r.cpu + start:04X} (+{start}): the ROM has `{want}` "
            f"({r.code[start:start + wl].hex(' ')}), yours `{have}` ({got[start:start + hl].hex(' ')}); {at} of "
            f"{len(r.code)} bytes match before it")


def _shift_checks(r: Routine, layout, shifted: bytes, hw_shifted: bytes) -> list[str]:
    out = []
    for it, off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, positions = operand
        if role == "imm16" and (value < 0x100 or "number" in it.comment.lower()):
            continue
        moved = shifted if not _hardware(role, value) else hw_shifted
        if all(moved[off + p] == r.code[off + p] for p in positions):
            if moved is hw_shifted:
                out.append(f"5. line {it.line}: `{it.text}`: ${value:04X} is hardware: use its hardware.inc name "
                           f"({gb_hw.registers().get(value) or gb_hw.mapper_registers(r.mapper).get(value) or gb_hw.area(value) or 'a register or area, plus an offset'})")
            else:
                out.append(f"2. line {it.line}: `{it.text}`: ${value:04X} is an address left as a number: name it "
                           "(a label, the project's name for it, or DEF NAME EQU $" f"{value:04X}" ")"
                           + (", or mark a constant that isn't one `; number`" if role == "imm16" else ""))
    return out


def _bank_checks(r: Routine, layout, symbols: dict[str, int]) -> list[str]:
    """3: a call, jump or pointer into the switchable bank names a function in the bank that's mapped there."""
    out = []
    banks_at: dict[int, set[int]] = {}
    for flat in r.functions:
        if flat >= BANK:
            banks_at.setdefault(BANK + flat % BANK, set()).add(flat // BANK)
    bank_of = {name: flat // BANK for flat, name in r.functions.items()}
    for it, off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, _positions = operand
        if not 0x4000 <= value < 0x8000 or role in ("write", "hmem"):
            continue
        used = [n for n in re.findall(_IDENT, it.text) if n in bank_of]
        if not used:
            continue
        if r.bank:
            want = r.bank  # code in the window reaches its own bank there
        elif off in r.edges:
            want = r.edges[off] // BANK  # the bank the split followed this call into
        else:
            if len(banks_at.get(value, ())) > 1:
                out.append(f"3. line {it.line}: banks {', '.join(str(b) for b in sorted(banks_at[value]))} all have "
                           f"code at ${value:04X}, and which one this routine maps there isn't known yet: say what you "
                           "know (correct_split) or leave it for later")
            continue
        for name in used:
            if bank_of[name] != want:
                out.append(f"3. line {it.line}: {name} is in bank {bank_of[name]}, but at ${value:04X} this routine "
                           f"reaches bank {want}")
    return out


def _data_checks(r: Routine, layout) -> list[str]:
    """4: db and dw only over what the map has as data in the routine, or an instruction a branch lands inside."""
    data = [(lo - r.flat, lo - r.flat + n) for lo, n, _kind in r.data]
    inner = set()
    for _it, off, text, _ln in layout:
        kind, target = sm83.flow(text)
        if target is not None and kind in ("jump", "cjump", "call") and r.cpu <= target < r.cpu + len(r.code):
            inner.add(target - r.cpu)
    out = []
    for it, off, _t, size in layout:
        if it.kind not in ("db", "dw"):
            continue
        covered = all(any(lo <= k < hi for lo, hi in data) for k in range(off, off + size))
        entered = any(off < t < off + size for t in inner)  # (an instruction a branch lands in the middle of)
        if not covered and not entered:
            out.append(f"4. line {it.line}: `{it.text[:40]}` writes bytes the routine runs as data: write them as "
                       "instructions (db and dw are for its operands, tables, or an instruction a branch lands inside)")
    return out


def _name_checks(r: Routine, labels, header: list[str], layout) -> list[str]:
    out = []
    for n, name in labels:
        if AUTO.match(name.split(".")[-1] if "." in name[1:] else name):
            out.append(f"6. line {n}: `{name}` is an automatic name: say what it is")
    said = " ".join(header)
    if len(said) < 60 or len(header) < 2:
        out.append("6. before its label, say in comment lines what the routine does, what it takes and what it returns")
    writes_bank = any(it.kind == "insn" and (op := _operands(t, ln)) and op[0] == "write" and op[1] < 0x8000
                      for it, _off, t, ln in layout)
    if writes_bank and "bank" not in said.lower():
        out.append("6. it maps a bank: the comment before it says which, or whose")
    return out


def available() -> bool:
    from . import gb_mgbdis

    return gb_mgbdis.tools() is not None


# ---------------------------------------------------------------- a draft to start from

def _signed(n: int) -> int:
    return n - 0x100 if n >= 0x80 else n


def draft(r: Routine) -> str:
    """The routine as RGBDS assembly that rebuilds its bytes, with what the project knows filled in: its functions'
    and symbols' names, hardware.inc's for the hardware, `db` over the data the map has in it, and a local label at
    each branch target inside it. Automatic names and no comment yet: what's left for the agent to write."""
    names: dict[int, str] = {}
    for name, value in sorted(r.symbols.items()):
        names.setdefault(value, name)
    flats = {flat: name for flat, name in r.functions.items()}
    regs, cart = gb_hw.registers(), gb_hw.mapper_registers(r.mapper)
    data = sorted((lo - r.flat, lo - r.flat + n) for lo, n, _k in r.data)

    def in_data(off: int):
        return next(((lo, hi) for lo, hi in data if lo <= off < hi), None)

    # the instructions (and data) at their offsets, and where branches inside go
    pieces, off, inner = [], 0, set()
    while off < len(r.code):
        span = in_data(off)
        if span:
            end = min(span[1], len(r.code))
            pieces.append((off, "db " + ", ".join(f"${b:02x}" for b in r.code[off:end]), end - off, None, None))
            off = end
            continue
        text, ln = sm83.decode(r.code[off:off + 3], r.cpu + off)
        kind, target = sm83.flow(text)
        if target is not None and r.cpu <= target < r.cpu + len(r.code) and kind in ("jump", "cjump", "call"):
            inner.add(target - r.cpu)
        pieces.append((off, text, ln, kind, target))
        off += ln
    starts = {p[0] for p in pieces}

    def code_name(target: int, off: int) -> str:
        if r.cpu <= target < r.cpu + len(r.code) and target - r.cpu in starts:
            return f".l_{target:04x}"
        if target < BANK:
            flat = target
        elif r.bank:
            flat = r.bank * BANK + target - BANK
        else:
            flat = r.edges.get(off)
        return flats.get(flat) or f"${target:04x}"

    def address(value: int, role: str) -> str:
        if role != "write" and (0xFF00 <= value <= 0xFFFF) and value in regs:
            return regs[value]
        if role == "write" and value in cart:
            return cart[value]
        for base, name in ((0x9C00, "TILEMAP1"), (0x9800, "TILEMAP0"), (0x8000, "_VRAM"), (0xFE00, "_OAMRAM"),
                           (0xFF00, "_IO")):  # video memory, OAM and unnamed I/O: an area plus an offset
            if base <= value < {0x9C00: 0xA000, 0x9800: 0x9C00, 0x8000: 0x9800, 0xFE00: 0xFEA0, 0xFF00: 0xFF80}[base]:
                return name if value == base else f"{name} + ${value - base:02x}"
        if value >= 0x8000 and value in names:
            return names[value]
        return f"${value:04x}"

    lines = []
    for off, text, ln, kind, target in pieces:
        if off in inner:
            lines.append(f".l_{r.cpu + off:04x}:")
        if kind is None:
            lines.append("    " + text)
            continue
        out = text
        if (m := re.match(r"^(jp|jr|call)( (?:n?[zc]), | )\$([0-9a-f]{4})$", text)):
            out = f"{m.group(1)}{m.group(2)}{code_name(int(m.group(3), 16), off)}"
        elif (operand := _operands(text, ln)) is not None:
            role, value, _p = operand
            if role == "hmem":
                out = re.sub(r"\(\$ff00\+\$[0-9a-f]{2}\)", f"[{address(value, role)}]", text)
                if not out.startswith("ldh"):
                    out = "ldh" + out[2:]
            elif role in ("read", "write"):
                out = re.sub(r"\(\$[0-9a-f]{4}\)", f"[{address(value, role)}]", text)
            elif role == "imm16":
                out = re.sub(r"\$[0-9a-f]{4}$", address(value, role) if value >= 0x100 else f"${value:04x}", text)
            elif role == "code":
                out = re.sub(r"\$[0-9a-f]{4}$", code_name(value, off), text)
        out = out.replace("($ff00+c)", "[c]").replace("(hl+)", "[hl+]").replace("(hl-)", "[hl-]")
        out = re.sub(r"\((hl|bc|de)\)", r"[\1]", out)
        if out.startswith("ld [c], a") or out.startswith("ld a, [c]"):
            out = "ldh" + out[2:]
        if (m := re.match(r"^(ld hl, sp\+|add sp, )\$([0-9a-f]{2})$", out)):
            n = _signed(int(m.group(2), 16))
            out = (f"ld hl, sp {'+' if n >= 0 else '-'} {abs(n)}" if out.startswith("ld") else f"add sp, {n}")
        if out.startswith("stop"):
            out = "stop"
        lines.append("    " + out)
    return f"; (say what it does)\n{r.name}:\n" + "\n".join(lines) + "\n"
