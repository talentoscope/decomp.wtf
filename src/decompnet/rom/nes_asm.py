"""Judging a hand-written NES routine an agent documented: the assembly track, as on the Game Boy (gb_asm.py) and the
Master System (sms_asm.py), in ca65, the assembler NES disassemblies are written for.

A routine is accepted when all of these hold:

1. **Byte-identical.** Assembled at the address it runs at (ca65 with `.org`, linked by ld65, with the hardware's
   names and the project's defined), it's the ROM's bytes there, no more and no fewer. ca65 picks zero-page
   addressing for an address below $100: where the game used the 3-byte form, write `a:` (`lda a:wTimer`).
2. **It survives a shift test.** Assembled again with the routine and every address the project knows moved (zero
   page kept in zero page, RAM in RAM), each operand that is an address (a call or jump target, a memory operand, a
   branch out of the routine) changes: a raw address left in doesn't.
3. **Names from the right bank.** A call or jump into a switched window names a function in the bank mapped there:
   the routine's own, when it's the window it runs from. Where banks share the address and no one knows which is
   mapped, it waits until someone does.
4. **Instructions stay instructions.** `.byte` and `.word` only where the map has data in the routine (a jump
   engine's table, a routine's operands) or an instruction a branch lands in the middle of.
5. **Hardware by name.** Assembled once more with the hardware's names moved, each operand that is hardware (the
   PPU's and APU's registers, the joypads, a mapper's registers) changes: `sta PPUCTRL`, not `sta $2000` (nes_hw.py).
6. **Real names and a header comment.** The routine's label and its cheap local labels (`@loop`) aren't automatic
   ones (`func_007_c123`, `@l_c130`), and comment lines before it say what it does (with the bank it maps, when it
   maps one).

`NAME = $number` lines name addresses (and numbers) for the project. Directives that read files, define macros or
change how the rest assembles (.include, .incbin, .macro, .segment, .org...) are refused: agents' text is assembled
on the user's computer.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import mos6502, nes_hw
from .gb_asm import AsmError, Verdict, _data_size, _split_comment

MAX_TEXT = 64 * 1024
SHIFT = 0x10
_IDENT = r"[A-Za-z_]\w*"
# a label: `Name:` (the routine's, or a second entry), `@name:` (a cheap local one), or ca65's unnamed `:`
_LABEL = re.compile(rf"^\s*(@?{_IDENT}|)(:)(?!=)\s*")
_NUMBER = r"(?:\$[0-9A-Fa-f]+|%[01]+|\d+)"
_DEFINE = re.compile(rf"^\s*({_IDENT})\s*=\s*({_NUMBER})\s*$")
_DATA = re.compile(r"^\.(byte|byt|word|addr)\b", re.I)
AUTO = re.compile(r"^@?(?:(?:func|sub|loc|label|lbl|l|unk|unknown|jmp|jsr|bra)_?[0-9A-Fa-f_]*|.*_[0-9A-Fa-f]{4})$", re.I)
_WRITES = ("sta", "stx", "sty", "inc", "dec", "asl", "lsr", "rol", "ror")


@dataclass
class Routine:
    """What the judge needs to know about the function an agent documented. Offsets are file offsets."""

    name: str
    bank: int
    cpu: int
    code: bytes
    symbols: dict[str, int]
    functions: dict[int, str] = field(default_factory=dict)  # file offset -> name, every function of the project
    runs_at: dict[int, int] = field(default_factory=dict)    # file offset -> the address it runs at
    banks: dict[int, int] = field(default_factory=dict)      # file offset -> its bank
    data: list[tuple[int, int, str]] = field(default_factory=list)  # (file offset, size, kind): data regions in it
    flat: int = 0                    # the routine's file offset
    window: int = 0x2000             # the size of the mapper's banks (a switched window)
    mapper: int = -1


@dataclass
class _Item:
    line: int
    kind: str                        # insn | db | dw | def
    text: str
    comment: str
    size: int = 0


def _number(text: str) -> int:
    t = text.strip()
    return int(t[1:], 16) if t.startswith("$") else int(t[1:], 2) if t.startswith("%") else int(t)


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
        if (m := _DEFINE.match(code)):
            items.append(_Item(n, "def", code.strip(), comment))
            continue
        while (m := _LABEL.match(code)) and (m.group(1) or code.strip().startswith(":")):
            labels.append((n, m.group(1)))
            code = code[m.end():]
        stmt = code.strip()
        if not stmt:
            continue
        if (m := _DATA.match(stmt)):
            kind = "db" if m.group(1).lower() in ("byte", "byt") else "dw"
            items.append(_Item(n, kind, stmt, comment, _data_size(kind, stmt[m.end():])))
            continue
        if stmt.startswith(".") or re.search(r"[{}\\]|:=", stmt):
            problems.append(f"line {n}: `{stmt.split()[0]}` isn't taken here: instructions, labels, .byte/.word and "
                            "NAME = $number only")
            continue
        items.append(_Item(n, "insn", stmt, comment))
    if not any(i.kind != "def" for i in items):
        problems.append("there's no code in it")
    return items, labels, header, problems


def contained(text: str) -> bool:
    try:
        return not any("isn't taken here" in p for p in parse(text)[3])
    except AsmError:
        return False


# ---------------------------------------------------------------- assembling

def _tools() -> tuple[Path, Path]:
    from .. import toolchains

    if toolchains.installed("cc65") is None:
        raise AsmError("cc65 (ca65 and ld65) isn't installed (Setup lists it)")
    base = toolchains.path("cc65")
    exe = ".exe" if (base / "ca65.exe").exists() else ""
    return base / f"ca65{exe}", base / f"ld65{exe}"


def available() -> bool:
    try:
        _tools()
        return True
    except AsmError:
        return False


def _assemble(tmp: Path, r: Routine, lines: list[str], symbols: dict[str, int], at: int, hw_moved: bool) -> bytes:
    """The text assembled at CPU address `at`: its bytes."""
    from .gb_build import _run

    asm, link = _tools()
    head = (nes_hw.inc(r.mapper, moved=hw_moved)
            + "".join(f"{n} = ${v & 0xFFFF:04X}\n" for n, v in sorted(symbols.items()))
            + f'.segment "CODE"\n.org ${at:04X}\n')
    offset = head.count("\n")
    (tmp / "routine.s").write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    (tmp / "link.cfg").write_text(f"MEMORY {{ ROM: start = ${at:04X}, size = $10000, file = %O, fill = no; }}\n"
                                  "SEGMENTS { CODE: load = ROM, type = ro; }\n", encoding="utf-8")
    for f in ("routine.o", "out.bin"):
        (tmp / f).unlink(missing_ok=True)
    rc, log = _run([asm, "-o", "routine.o", "routine.s"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset))
    rc, log = _run([link, "-C", "link.cfg", "-o", "out.bin", "routine.o"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset))
    return (tmp / "out.bin").read_bytes()


def _clean(log: str, offset: int) -> str:
    out = []
    for line in log.splitlines():
        line = re.sub(r"\S*routine\.s\((\d+)\)", lambda m: f"line {int(m.group(1)) - offset}", line)
        line = re.sub(r"\S*/(\w+\.(?:s|o|cfg))", r"\1", line)
        if line.strip():
            out.append(line.strip())
    text = "\n".join(out[-8:]) or "the assembler refused it"
    if (m := re.search(r"Symbol '([^']+)' is undefined", text)):
        text += (f"\n({m.group(1)} isn't a name the project has: use one it has (the brief's draft does), or name the "
                 f"address yourself with {m.group(1)} = $XXXX)")
    return text


def _moved(value: int, delta: int) -> int:
    """An address the shift test moves: zero page in zero page, RAM in RAM, cartridge RAM and ROM by `delta`; not
    hardware."""
    if value < 0x100:
        return value + SHIFT if value + SHIFT < 0x100 else value - SHIFT
    if value < 0x800:
        return value + SHIFT if value + SHIFT < 0x800 else value - SHIFT
    if 0x6000 <= value <= 0xFFFF:
        moved = value + delta
        return moved if 0x6000 <= moved <= 0xFFFF else value - delta
    return value


# ---------------------------------------------------------------- the checks

_BRANCH = re.compile(r"^(?:bcc|bcs|beq|bne|bmi|bpl|bvc|bvs) \$([0-9a-f]{4})$")


def _operands(text: str, ln: int):
    """(role, value, byte positions) for an instruction's address operand, or None. Roles: code (jsr, jmp), rel (a
    branch), read / write (a memory operand), zp (a zero-page one)."""
    op, _, rest = text.partition(" ")
    if (m := _BRANCH.match(text)):
        return "rel", int(m.group(1), 16), (1,)
    if op in ("jsr", "jmp") and (m := re.match(r"^\$([0-9a-f]{4})$", rest)):
        return "code", int(m.group(1), 16), (1, 2)
    if op == "jmp" and (m := re.match(r"^\(\$([0-9a-f]{4})\)$", rest)):
        return "read", int(m.group(1), 16), (1, 2)
    if (m := re.match(r"^\$([0-9a-f]{4})(?:,[xy])?$", rest)) and ln == 3:
        return ("write" if op in _WRITES else "read"), int(m.group(1), 16), (1, 2)
    if (m := re.match(r"^\(?\$([0-9a-f]{2})(?:,x\)|\),y|,[xy])?$", rest)) and ln == 2:
        return "zp", int(m.group(1), 16), (1,)
    return None


def judge(r: Routine, text: str, tmp_root: Path | None = None) -> Verdict:
    """Whether `text` documents the routine (see the module's six checks)."""
    items, labels, header, problems = parse(text)
    globals_ = [name for _n, name in labels if re.fullmatch(_IDENT, name)]
    label = globals_[0] if globals_ else ""
    if problems:
        return Verdict(False, 0, 0, problems, label)
    if not label:
        return Verdict(False, 0, 0, ["give the routine its label (its name) before its first instruction"], label)
    hw = set(nes_hw.names(r.mapper))
    defines = {_DEFINE.match(i.text).group(1): _number(_DEFINE.match(i.text).group(2)) for i in items if i.kind == "def"}
    own = set(globals_) | set(defines)
    taken = [n for n in globals_ if n in r.symbols and n != r.name and not r.cpu <= r.symbols[n] < r.cpu + len(r.code)]
    if taken:
        return Verdict(False, 0, 0, [f"6. {', '.join(taken)} already name{'s' if len(taken) == 1 else ''} something else "
                                     "in the project: pick another name"], label)
    if (clash := sorted(own & hw)):
        return Verdict(False, 0, 0, [f"{', '.join(clash)} already name{'s' if len(clash) == 1 else ''} hardware "
                                     "(nes_hw): pick another name"], label)
    clash = [n for n, v in defines.items() if n in r.symbols and r.symbols[n] != v]
    if clash:
        return Verdict(False, 0, 0, [f"2. {', '.join(clash)} already name{'s' if len(clash) == 1 else ''} another "
                                     "address in the project"], label)
    symbols = {n: v for n, v in r.symbols.items() if n not in own and n not in hw and re.fullmatch(_IDENT, n)}
    lines = text.splitlines()
    with tempfile.TemporaryDirectory(prefix="nes-asm-", dir=tmp_root) as tmp:
        tmp = Path(tmp)
        try:
            got = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=False)
        except AsmError as e:
            return Verdict(False, 0, 0, [f"1. it doesn't assemble: {e}"], label)
        same = next((k for k, (a, b) in enumerate(zip(got, r.code)) if a != b), min(len(got), len(r.code)))
        if got != r.code:
            return Verdict(False, same, len(got), [_difference(r, got, same)], label)
        layout, off = [], 0
        for it in items:
            if it.kind == "def":
                continue
            if it.kind == "insn":
                t, ln = mos6502.decode(r.code[off:off + 3], r.cpu + off)
                if t.startswith("brk") and it.text.strip().lower() == "brk":
                    t, ln = "brk", 1  # (ca65's brk is its opcode alone: the byte after it is written on its own)
                layout.append((it, off, t, ln))
                off += ln
            else:
                layout.append((it, off, it.kind, it.size))
                off += it.size
        if off != len(r.code):
            return Verdict(False, len(r.code), len(got), [
                "4. its lines don't line up with the bytes one statement a line: write one instruction a line, .byte "
                "and .word for data"], label)
        start = r.cpu & ~(r.window - 1)
        delta = SHIFT if r.cpu + len(r.code) + SHIFT <= start + r.window else -SHIFT
        moved_lines = list(lines)
        for it in items:
            if it.kind == "def":
                name = _DEFINE.match(it.text).group(1)
                moved_lines[it.line - 1] = f"{name} = ${_moved(defines[name], delta) & 0xFFFF:04X}"
        try:
            shifted = _assemble(tmp, r, moved_lines, {n: _moved(v, delta) for n, v in symbols.items()}, r.cpu + delta,
                                hw_moved=False)
            hw_shifted = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=True)
        except AsmError as e:
            return Verdict(False, len(r.code), len(got), [f"2. it doesn't assemble moved (the shift test): {e}"], label)
    problems += _shift_checks(r, layout, shifted, hw_shifted)
    problems += _bank_checks(r, layout)
    problems += _data_checks(r, layout)
    problems += _name_checks(labels, header, layout, r)
    kept = "\n".join(line for k, line in enumerate(lines, 1)
                     if not any(i.kind == "def" and i.line == k for i in items))
    return Verdict(not problems, len(r.code), len(got), sorted(problems), label, defines, kept.strip("\n") + "\n")


def _difference(r: Routine, got: bytes, at: int) -> str:
    # an address below $100 written in zero page where the game has the 3-byte form: every byte after it moves, so
    # that's the one to say
    want_insns = list(mos6502.disassemble(r.code, r.cpu))
    have_insns = list(mos6502.disassemble(got, r.cpu))
    for (wa, wt, wl), (_ha, ht, hl) in zip(want_insns, have_insns):
        if wl == 3 and hl == 2 and wt.split(" ")[0] == ht.split(" ")[0]:
            off = wa - r.cpu
            return (f"1. `{wt}` at ${wa:04X} (+{off}) is 3 bytes in the ROM, yours `{ht}` is 2: ca65 writes an address "
                    "below $100 in zero page; `a:` keeps the 3-byte form (`lda a:zName`)")
        if wl != hl:
            break
    if len(got) != len(r.code) and at >= min(len(got), len(r.code)):
        return (f"1. it assembles to {len(got)} bytes, the routine is {len(r.code)}: "
                + ("it stops early" if len(got) < len(r.code) else "it runs past the routine's end"))
    start = 0
    for addr, _text, ln in mos6502.disassemble(r.code, r.cpu):
        if addr - r.cpu + ln > at:
            start = addr - r.cpu
            break
    want, wl = mos6502.decode(r.code[start:start + 3], r.cpu + start)
    have, hl = mos6502.decode(got[start:start + 3], r.cpu + start)
    return (f"1. the bytes differ at ${r.cpu + start:04X} (+{start}): the ROM has `{want}` "
            f"({r.code[start:start + wl].hex(' ')}), yours `{have}` ({got[start:start + hl].hex(' ')}); {at} of "
            f"{len(r.code)} bytes match before it")


def _shift_checks(r: Routine, layout, shifted: bytes, hw_shifted: bytes) -> list[str]:
    out = []
    for it, off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, positions = operand
        if role == "rel":
            if not r.cpu <= value < r.cpu + len(r.code) and shifted[off + 1] != r.code[off + 1]:
                out.append(f"2. line {it.line}: `{it.text}`: ${value:04X} is an address left as a number: name it "
                           "(a label, or the project's name for the function there)")
            continue
        write = role == "write"
        if nes_hw.hardware(value, write, r.mapper):
            if all(hw_shifted[off + p] == r.code[off + p] for p in positions):
                out.append(f"5. line {it.line}: `{it.text}`: ${value:04X} is hardware: use its name "
                           f"({nes_hw.name_of(value, write, r.mapper) or 'a register name, plus an offset'})")
        elif all(shifted[off + p] == r.code[off + p] for p in positions):
            width = 2 if role == "zp" else 4
            out.append(f"2. line {it.line}: `{it.text}`: ${value:0{width}X} is an address left as a number: name it "
                       f"(a label, the project's name for it, or NAME = ${value:0{width}X})")
    return out


def _bank_checks(r: Routine, layout) -> list[str]:
    """3: a call or jump into a switched window names a function in the bank that's mapped there."""
    out = []
    banks_at: dict[int, set[int]] = {}
    for flat, cpu in r.runs_at.items():
        banks_at.setdefault(cpu, set()).add(r.banks.get(flat, -1))
    bank_of = {name: r.banks.get(flat, -1) for flat, name in r.functions.items()}
    window = r.cpu & ~(r.window - 1)
    for it, _off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, _positions = operand
        if role != "code" or value < 0x8000:
            continue
        used = [n for n in re.findall(_IDENT, it.text) if n in bank_of]
        if not used:
            continue
        if value & ~(r.window - 1) == window:
            want = r.bank
        else:
            if len(banks_at.get(value, ())) > 1:
                out.append(f"3. line {it.line}: banks {', '.join(str(b) for b in sorted(banks_at[value]))} all have "
                           f"code at ${value:04X}, and which one is mapped there here isn't known yet: say what you "
                           "know, or leave it for later")
            continue
        for name in used:
            if bank_of[name] != want:
                out.append(f"3. line {it.line}: {name} is in bank {bank_of[name]}, but at ${value:04X} this routine "
                           f"reaches bank {want}")
    return out


def _data_checks(r: Routine, layout) -> list[str]:
    data = [(lo - r.flat, lo - r.flat + n) for lo, n, _kind in r.data]
    inner = set()
    for _it, _off, text, _ln in layout:
        kind, target = mos6502.flow(text)
        if target is not None and kind in ("jump", "cjump", "call") and r.cpu <= target < r.cpu + len(r.code):
            inner.add(target - r.cpu)
    out = []
    for k, (it, off, _t, size) in enumerate(layout):
        if it.kind not in ("db", "dw"):
            continue
        covered = all(any(lo <= j < hi for lo, hi in data) for j in range(off, off + size))
        entered = any(off < t < off + size for t in inner)
        after_brk = k > 0 and layout[k - 1][2] == "brk" and size == 1  # (brk's signature byte)
        if not covered and not entered and not after_brk:
            out.append(f"4. line {it.line}: `{it.text[:40]}` writes bytes the routine runs as data: write them as "
                       "instructions (.byte and .word are for its tables and operands, or an instruction a branch lands "
                       "inside)")
    return out


def _name_checks(labels, header: list[str], layout, r: Routine) -> list[str]:
    out = []
    for n, name in labels:
        if name and AUTO.match(name):
            out.append(f"6. line {n}: `{name}` is an automatic name: say what it is")
    said = " ".join(header)
    if len(said) < 60 or len(header) < 2:
        out.append("6. before its label, say in comment lines what the routine does, what it takes and what it returns")
    maps = any(it.kind == "insn" and (op := _operands(t, ln)) and op[0] == "write" and op[1] >= 0x8000
               for it, _off, t, ln in layout)
    if maps and "bank" not in said.lower():
        out.append("6. it writes to the mapper: the comment before it says what it maps, or whose")
    return out


# ---------------------------------------------------------------- a draft to start from

def draft(r: Routine) -> str:
    """The routine as ca65 assembly that rebuilds its bytes, with what the project knows filled in: its functions' and
    symbols' names (`a:` where the game used the 3-byte form for a zero-page address), the hardware's names, `.byte`
    over the data the map has in it, and a cheap local label at each branch target inside it. Automatic names and no
    comment yet: what's left for the agent to write."""
    names: dict[int, str] = {}
    for name, value in sorted(r.symbols.items()):
        names.setdefault(value, name)
    by_cpu: dict[int, list[int]] = {}
    for flat, cpu in r.runs_at.items():
        by_cpu.setdefault(cpu, []).append(flat)
    data = sorted((lo - r.flat, lo - r.flat + n) for lo, n, _k in r.data)
    window = r.cpu & ~(r.window - 1)

    def in_data(off: int):
        return next(((lo, hi) for lo, hi in data if lo <= off < hi), None)

    pieces, off, inner = [], 0, set()
    while off < len(r.code):
        span = in_data(off)
        if span:
            end = min(span[1], len(r.code))
            pieces.append((off, ".byte " + ", ".join(f"${b:02x}" for b in r.code[off:end]), end - off))
            off = end
            continue
        text, ln = mos6502.decode(r.code[off:off + 3], r.cpu + off)
        kind, target = mos6502.flow(text)
        if target is not None and r.cpu <= target < r.cpu + len(r.code) and kind in ("jump", "cjump", "call"):
            inner.add(target - r.cpu)
        pieces.append((off, text, ln))
        off += ln
    starts = {p[0] for p in pieces}

    def code_name(target: int) -> str:
        if r.cpu <= target < r.cpu + len(r.code) and target - r.cpu in starts:
            return f"@l_{target:04x}"
        if target & ~(r.window - 1) == window:
            flats = [f for f in by_cpu.get(target, []) if r.banks.get(f) == r.bank]
        else:
            flats = by_cpu.get(target, [])
        named = [r.functions[f] for f in flats if f in r.functions]
        return named[0] if len(named) == 1 else f"${target:04x}"

    lines = []
    for off, text, ln in pieces:
        if off in inner:
            lines.append(f"@l_{r.cpu + off:04x}:")
        if text.startswith((".byte", "db ")):
            lines.append("    " + (".byte " + text[3:] if text.startswith("db ") else text))
            continue
        if text.startswith("brk "):  # (ca65's brk is the opcode alone: the byte after it on its own)
            lines += ["    brk", f"    .byte {text[4:]}"]
            continue
        out = text
        operand = _operands(text, ln)
        if operand is not None:
            role, value, _p = operand
            write = role == "write"
            if role in ("code", "rel"):
                out = re.sub(r"\$[0-9a-f]{4}$", code_name(value), text)
            elif (name := nes_hw.name_of(value, write, r.mapper)) is not None and role != "zp":
                out = re.sub(r"\$[0-9a-f]{4}", name, text, count=1)
            elif value in names and role != "zp":
                wide = "a:" if value < 0x100 and not text.startswith("jmp (") else ""  # (jmp ( ) is always 2 bytes)
                out = re.sub(r"\$[0-9a-f]{4}", wide + names[value], text, count=1)
            elif value in names:
                out = re.sub(r"\$[0-9a-f]{2}\b", names[value], text, count=1)
            elif role != "zp" and value < 0x100 and not text.startswith("jmp ("):
                out = re.sub(r"\$([0-9a-f]{4})", r"a:$\1", text, count=1)
        lines.append("    " + out)
    return f"; (say what it does)\n{r.name}:\n" + "\n".join(lines) + "\n"
