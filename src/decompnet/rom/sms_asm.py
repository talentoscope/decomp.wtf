"""Judging a hand-written Master System or Game Gear routine an agent documented: the assembly track, as on the Game
Boy (gb_asm.py), in WLA-DX, the assembler these consoles' disassemblies are written for.

A routine is accepted when all of these hold:

1. **Byte-identical.** Assembled in its bank at the address it runs at (wla-z80 and wlalink, with the hardware's names
   and the project's symbols defined), it's the ROM's bytes there, no more and no fewer.
2. **It survives a shift test.** Assembled again with the routine and every address the project knows moved, each
   operand that is an address (a call or jump target, a memory operand, a 16-bit constant in ROM or RAM) changes: a
   raw address left in doesn't. A 16-bit constant that only looks like an address is marked `; number`.
3. **Names from the right bank.** A call, jump or pointer into slot 1 or 2 names a function in the bank mapped there:
   the routine's own, when it's the slot the routine runs from. Where banks share the address and no one knows which
   is mapped, it waits until someone does rather than accept a guess.
4. **Instructions stay instructions.** `.db` and `.dw` only where the map has data in the routine (a routine's
   operands after a call, a table) or an instruction a branch lands in the middle of.
5. **Hardware by name.** Assembled once more with the hardware's names moved, each operand that is hardware (an I/O
   port, a mapper register) changes: `out (PortVDPControl), a`, not `out ($bf), a` (sms_hw.py has the names).
6. **Real names and a header comment.** The routine's label and its child labels (`@loop`) aren't automatic ones
   (`func_005_8100`, `_LABEL_9D9_`, `@l_8a1c`), and comment lines before it say what it does (with the bank it maps,
   when it maps one).

`.DEFINE NAME $C0A2` lines name addresses (and numbers) for the project: they move with the shift test like the
project's own, and an accepted routine's become project symbols; their value is a number. Directives that read or
write files, define macros or change how the rest assembles (.INCLUDE, .INCBIN, .SECTION, .BANK, .ORG, .MACRO,
.REPT...) are refused: agents' text is assembled on the user's computer.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import sms_hw, z80
from .gb_asm import AsmError, Verdict, _data_size, _split_comment

BANK = 0x4000
MAX_TEXT = 64 * 1024
SHIFT = 0x10
_IDENT = r"[A-Za-z_]\w*"
# a label: `Name:` (the routine's, or a second entry into it), `@name:` (a child of the one before), or one of WLA-DX's
# anonymous ones (`-:`, `+:`, `__:`)
_LABEL = re.compile(rf"^\s*(@*{_IDENT}|[-+]+|__)(:)\s*")
_NUMBER = r"(?:\$[0-9A-Fa-f]+|0x[0-9A-Fa-f]+|[0-9A-Fa-f]+h|%[01]+|\d+)"
_DEFINE = re.compile(rf"^\s*\.(?:define|def|equ)\s+({_IDENT})\s+({_NUMBER})\s*$", re.I)
_DATA = re.compile(r"^\.(db|dw|byte|word)\b", re.I)
# automatic names: the split's, an emulator's or a disassembler's, or anything that only spells an address
AUTO = re.compile(r"^@*(?:(?:func|sub|loc|label|lbl|l|unk|unknown|call|jump|jr)_?[0-9A-Fa-f_]*|_?LABEL_[0-9A-Fa-f]+_?|"
                  r".*_[0-9A-Fa-f]{4})$", re.I)


@dataclass
class Routine:
    """What the judge needs to know about the function an agent documented."""

    name: str
    bank: int
    cpu: int                         # the address it runs at (its slot: cpu >> 14)
    code: bytes
    symbols: dict[str, int]          # the project's names -> addresses
    functions: dict[int, str] = field(default_factory=dict)  # ROM offset -> name, every function of the project
    runs_at: dict[int, int] = field(default_factory=dict)    # ROM offset -> the address it runs at
    data: list[tuple[int, int, str]] = field(default_factory=list)  # (ROM offset, size, kind): data regions in it
    banks: int = 8

    @property
    def flat(self) -> int:
        return flat_of(self.bank, self.cpu)

    @property
    def slot(self) -> int:
        return self.cpu >> 14


def flat_of(bank: int, cpu: int) -> int:
    return cpu if bank == 0 and cpu < BANK else bank * BANK + (cpu & (BANK - 1))


@dataclass
class _Item:
    line: int
    kind: str                        # insn | db | dw | def
    text: str
    comment: str
    size: int = 0


def _number(text: str) -> int:
    t = text.strip()
    if t.startswith("$"):
        return int(t[1:], 16)
    if t.lower().startswith("0x"):
        return int(t[2:], 16)
    if t.lower().endswith("h"):
        return int(t[:-1], 16)
    if t.startswith("%"):
        return int(t[1:], 2)
    return int(t)


# ---------------------------------------------------------------- reading the text

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
        if (m := _DEFINE.match(stmt)):
            items.append(_Item(n, "def", stmt, comment))
            continue
        if (m := _DATA.match(stmt)):
            kind = "db" if m.group(1).lower() in ("db", "byte") else "dw"
            items.append(_Item(n, kind, stmt, comment, _data_size(kind, stmt[m.end():])))
            continue
        if stmt.startswith(".") or re.search(r"[{}\\]", stmt):
            problems.append(f"line {n}: `{stmt.split()[0]}` isn't taken here: instructions, labels, .db/.dw and "
                            ".DEFINE NAME $number only")
            continue
        items.append(_Item(n, "insn", stmt, comment))
    if not any(i.kind != "def" for i in items):
        problems.append("there's no code in it")
    return items, labels, header, problems


def contained(text: str) -> bool:
    """Whether text is only what the judge takes (a room member checks what its host sends before keeping it)."""
    try:
        return not any("isn't taken here" in p for p in parse(text)[3])
    except AsmError:
        return False


# ---------------------------------------------------------------- assembling

def _tools() -> tuple[Path, Path]:
    from .. import toolchains

    if toolchains.installed("wla-dx") is None:
        raise AsmError("WLA-DX isn't installed (Setup lists it)")
    base = toolchains.path("wla-dx")
    exe = ".exe" if (base / "wla-z80.exe").exists() else ""
    return base / f"wla-z80{exe}", base / f"wlalink{exe}"


def available() -> bool:
    try:
        _tools()
        return True
    except AsmError:
        return False


def _maps(r: Routine) -> str:
    banks = max(r.banks, r.bank + 1, 3)
    return (".MEMORYMAP\nDEFAULTSLOT 0\nSLOT 0 $0000 $4000\nSLOT 1 $4000 $4000\nSLOT 2 $8000 $4000\n.ENDME\n"
            f".ROMBANKMAP\nBANKSTOTAL {banks}\nBANKSIZE $4000\nBANKS {banks}\n.ENDRO\n")


def _wrapper(r: Routine, symbols: dict[str, int], at: int, hw_moved: bool) -> str:
    return (_maps(r) + sms_hw.inc(moved=hw_moved)
            + "".join(f".DEFINE {n} ${v & 0xFFFF:04X}\n" for n, v in sorted(symbols.items()))
            + f".BANK {r.bank} SLOT {at >> 14}\n.ORG ${at & (BANK - 1):04X}\n.SECTION \"routine\" FORCE\n")


def _labels(r: Routine, labels: dict[int, str], delta: int) -> str:
    """The project's functions as labels where they run (WLA-DX's `jr` and `djnz` take a label, not a number), moved
    by `delta` for the shift test (staying in their slot)."""
    out = [_maps(r)]
    for flat, name in sorted(labels.items()):
        cpu = r.runs_at.get(flat)
        if cpu is None:
            continue
        moved = cpu + delta if (cpu + delta) >> 14 == cpu >> 14 and cpu + delta >= 0 else cpu - delta
        out.append(f".BANK {flat // BANK if flat >= BANK else 0} SLOT {moved >> 14}\n.ORG ${moved & (BANK - 1):04X}\n"
                   f"{name}:\n")
    return "".join(out)


def _assemble(tmp: Path, r: Routine, lines: list[str], symbols: dict[str, int], at: int, hw_moved: bool,
              labels: dict[int, str] | None = None, delta: int = 0) -> bytes:
    """The text assembled at CPU address `at` in the routine's bank: the bytes of its section. `labels`: the
    project's functions to define as labels (ROM offset -> name), moved by `delta`."""
    from .gb_build import _run

    asm, link = _tools()
    head = _wrapper(r, symbols, at, hw_moved)
    offset = head.count("\n")
    (tmp / "routine.asm").write_text(head + "\n".join(lines) + "\n.ENDS\n", encoding="utf-8")
    (tmp / "names.asm").write_text(_labels(r, labels or {}, delta), encoding="utf-8")
    (tmp / "link.txt").write_text("[objects]\nroutine.o\nnames.o\n", encoding="utf-8")
    for f in ("routine.o", "names.o", "out.sms", "out.sym"):
        (tmp / f).unlink(missing_ok=True)
    rc, log = _run([asm, "-o", "routine.o", "routine.asm"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset))
    rc, log = _run([asm, "-o", "names.o", "names.asm"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset))
    rc, log = _run([link, "-S", "-r", "link.txt", "out.sms"], tmp, timeout=30)
    if rc != 0:
        raise AsmError(_clean(log, offset))
    size = 0
    for line in (tmp / "out.sym").read_text(encoding="utf-8", errors="replace").splitlines():
        if (m := re.match(r"^[0-9a-fA-F]{8}\s+[0-9a-fA-F]+:[0-9a-fA-F]{4}\s+[0-9a-fA-F]{4}\s+([0-9a-fA-F]{8})\s+routine$",
                          line.strip())):
            size = int(m.group(1), 16)
    flat = flat_of(r.bank, at)
    return (tmp / "out.sms").read_bytes()[flat:flat + size]


def _clean(log: str, offset: int) -> str:
    """WLA-DX's message for the agent: its own text's line numbers, no paths."""
    out = []
    for line in log.splitlines():
        line = re.sub(r"\S*routine\.asm:(\d+)", lambda m: f"line {int(m.group(1)) - offset}", line)
        line = re.sub(r"\S*/(\w+\.(?:asm|o|txt))", r"\1", line).removeprefix("routine.o: ")
        if line.strip() and not line.startswith("---"):
            out.append(line.strip())
    text = "\n".join(out[-8:]) or "the assembler refused it"
    if (m := re.search(r'unknown label "([^"]+)"', text)):
        text += (f"\n({m.group(1)} isn't a name the project has: use one it has (the brief's draft does), or name the "
                 f"address yourself with .DEFINE {m.group(1)} $XXXX)")
    return text


def _moved(value: int, delta: int) -> int:
    """An address the shift test moves: ROM and RAM ones by `delta` (RAM kept in RAM); not numbers, not hardware."""
    if 0x100 <= value < 0xC000:
        return (value + delta) & 0xFFFF
    if 0xC000 <= value < 0xFFFC:
        moved = value + delta
        return moved if 0xC000 <= moved < 0xFFFC else value - delta
    return value


# ---------------------------------------------------------------- the checks

_CONDITION = r"(?:nz|z|nc|c|po|pe|p|m)"


def _operands(text: str, ln: int):
    """(role, value, byte positions) for an instruction's address-like operand, or None. Roles: code (an absolute
    call or jump target), read / write (a memory operand), imm16 (a 16-bit constant), port (an I/O port)."""
    if (m := re.match(rf"^(?:call|jp)(?: {_CONDITION},)? \$([0-9a-f]{{4}})$", text)):
        return "code", int(m.group(1), 16), (ln - 2, ln - 1)
    if (m := re.match(r"^ld \(\$([0-9a-f]{4})\), (?:a|hl|bc|de|sp|ix|iy)$", text)):
        return "write", int(m.group(1), 16), (ln - 2, ln - 1)
    if (m := re.match(r"^ld (?:a|hl|bc|de|sp|ix|iy), \(\$([0-9a-f]{4})\)$", text)):
        return "read", int(m.group(1), 16), (ln - 2, ln - 1)
    if (m := re.match(r"^ld (?:bc|de|hl|sp|ix|iy), \$([0-9a-f]{4})$", text)):
        return "imm16", int(m.group(1), 16), (ln - 2, ln - 1)
    if (m := re.match(r"^(?:in a, \(\$([0-9a-f]{2})\)|out \(\$([0-9a-f]{2})\), a)$", text)):
        return "port", int(m.group(1) or m.group(2), 16), (1,)
    if (m := re.match(rf"^(?:jr|djnz)(?: {_CONDITION},)? \$([0-9a-f]{{4}})$", text)):
        return "rel", int(m.group(1), 16), (1,)  # (relative: a name moves with the routine, a number doesn't)
    return None


def _hardware(role: str, value: int) -> bool:
    """Whether an operand names hardware (checked by moving its names), not an address of the game's."""
    return role == "port" or value >= 0xFFFC or (role == "write" and value < 0xC000)  # (a write to ROM: a mapper's)


def judge(r: Routine, text: str, tmp_root: Path | None = None) -> Verdict:
    """Whether `text` documents the routine (see the module's six checks)."""
    items, labels, header, problems = parse(text)
    globals_ = [name for _n, name in labels if re.fullmatch(_IDENT, name) and name != "__"]
    label = globals_[0] if globals_ else ""
    if problems:
        return Verdict(False, 0, 0, problems, label)
    if not label:
        return Verdict(False, 0, 0, ["give the routine its label (its name) before its first instruction"], label)
    hw = set(sms_hw.names())
    defines = {_DEFINE.match(i.text).group(1): _number(_DEFINE.match(i.text).group(2)) for i in items if i.kind == "def"}
    own = set(globals_) | set(defines)
    taken = [n for n in globals_ if n in r.symbols and n != r.name and not r.cpu <= r.symbols[n] < r.cpu + len(r.code)]
    if taken:
        return Verdict(False, 0, 0, [f"6. {', '.join(taken)} already name{'s' if len(taken) == 1 else ''} something else "
                                     "in the project: pick another name"], label)
    if (clash := sorted(own & hw)):
        return Verdict(False, 0, 0, [f"{', '.join(clash)} already name{'s' if len(clash) == 1 else ''} hardware "
                                     "(sms_hw): pick another name"], label)
    clash = [n for n, v in defines.items() if n in r.symbols and r.symbols[n] != v]
    if clash:
        return Verdict(False, 0, 0, [f"2. {', '.join(clash)} already name{'s' if len(clash) == 1 else ''} another "
                                     "address in the project"], label)
    placed = {flat: name for flat, name in r.functions.items() if name not in own and re.fullmatch(_IDENT, name)}
    named = set(placed.values())  # (labels where they run; every other name of the project is a definition)
    symbols = {n: v for n, v in r.symbols.items()
               if n not in own and n not in hw and n not in named and re.fullmatch(_IDENT, n)}
    lines = text.splitlines()
    with tempfile.TemporaryDirectory(prefix="sms-asm-", dir=tmp_root) as tmp:
        tmp = Path(tmp)
        try:
            got = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=False, labels=placed)
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
                t, ln = z80.decode(r.code[off:off + 4], r.cpu + off)
                layout.append((it, off, t, ln))
                off += ln
            else:
                layout.append((it, off, it.kind, it.size))
                off += it.size
        if off != len(r.code):
            return Verdict(False, len(r.code), len(got), [
                "4. its lines don't line up with the bytes one statement a line: write one instruction a line, .db and "
                ".dw for data"], label)
        delta = SHIFT if (r.cpu & (BANK - 1)) + len(r.code) + SHIFT <= BANK else -SHIFT
        moved_lines = list(lines)
        for it in items:  # its own names, moved like the project's
            if it.kind == "def":
                name, value = _DEFINE.match(it.text).group(1), defines[_DEFINE.match(it.text).group(1)]
                moved_lines[it.line - 1] = f".DEFINE {name} ${_moved(value, delta) & 0xFFFF:04X}"
        try:
            shifted = _assemble(tmp, r, moved_lines, {n: _moved(v, delta) for n, v in symbols.items()}, r.cpu + delta,
                                hw_moved=False, labels=placed, delta=delta)
            hw_shifted = _assemble(tmp, r, lines, symbols, r.cpu, hw_moved=True, labels=placed)
        except AsmError as e:
            return Verdict(False, len(r.code), len(got), [f"2. it doesn't assemble moved (the shift test): {e}"], label)
    problems += _shift_checks(r, layout, shifted, hw_shifted)
    problems += _bank_checks(r, layout)
    problems += _data_checks(r, layout)
    problems += _name_checks(labels, header, layout)
    kept = "\n".join(line for k, line in enumerate(lines, 1)
                     if not any(i.kind == "def" and i.line == k for i in items))
    return Verdict(not problems, len(r.code), len(got), sorted(problems), label, defines, kept.strip("\n") + "\n")


def _difference(r: Routine, got: bytes, at: int) -> str:
    if len(got) != len(r.code) and at >= min(len(got), len(r.code)):
        return (f"1. it assembles to {len(got)} bytes, the routine is {len(r.code)}: "
                + ("it stops early" if len(got) < len(r.code) else "it runs past the routine's end"))
    start = 0
    for addr, _text, ln in z80.disassemble(r.code, r.cpu):
        if addr - r.cpu + ln > at:
            start = addr - r.cpu
            break
    want, wl = z80.decode(r.code[start:start + 4], r.cpu + start)
    have, hl = z80.decode(got[start:start + 4], r.cpu + start)
    return (f"1. the bytes differ at ${r.cpu + start:04X} (+{start}): the ROM has `{want}` "
            f"({r.code[start:start + wl].hex(' ')}), yours `{have}` ({got[start:start + hl].hex(' ')}); {at} of "
            f"{len(r.code)} bytes match before it")


def _hardware_hint(role: str, value: int, text: str) -> str:
    if role == "port":
        return sms_hw.port_name(value, write=text.startswith("out")) or "a port's name from sms_hw, plus an offset"
    return sms_hw.MAPPER.get(value, "a mapper register's name")


def _shift_checks(r: Routine, layout, shifted: bytes, hw_shifted: bytes) -> list[str]:
    out = []
    for it, off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, positions = operand
        if role == "imm16" and (value < 0x100 or "number" in it.comment.lower()):
            continue
        if role == "rel":  # a relative jump: to a name, the offset stays as the routine moves; to a number, it doesn't
            if not r.cpu <= value < r.cpu + len(r.code) and shifted[off + 1] != r.code[off + 1]:
                out.append(f"2. line {it.line}: `{it.text}`: ${value:04X} is an address left as a number: name it "
                           "(a label, or the project's name for the function there)")
            continue
        moved = hw_shifted if _hardware(role, value) else shifted
        if all(moved[off + p] == r.code[off + p] for p in positions):
            if moved is hw_shifted:
                out.append(f"5. line {it.line}: `{it.text}`: ${value:0{2 if role == 'port' else 4}X} is hardware: use "
                           f"its name ({_hardware_hint(role, value, text)})")
            else:
                out.append(f"2. line {it.line}: `{it.text}`: ${value:04X} is an address left as a number: name it "
                           f"(a label, the project's name for it, or .DEFINE NAME ${value:04X})"
                           + (", or mark a constant that isn't one `; number`" if role == "imm16" else ""))
    return out


def _bank_checks(r: Routine, layout) -> list[str]:
    """3: a call, jump or pointer into slot 1 or 2 names a function in the bank that's mapped there."""
    out = []
    banks_at: dict[int, set[int]] = {}
    for flat, cpu in r.runs_at.items():
        if flat >= BANK:
            banks_at.setdefault(cpu, set()).add(flat // BANK)
    bank_of = {name: flat // BANK for flat, name in r.functions.items()}
    for it, _off, text, ln in layout:
        if it.kind != "insn" or (operand := _operands(text, ln)) is None:
            continue
        role, value, _positions = operand
        if not 0x4000 <= value < 0xC000 or role in ("write", "port", "rel"):
            continue
        used = [n for n in re.findall(_IDENT, it.text) if n in bank_of]
        if not used:
            continue
        if value >> 14 == r.slot:
            want = r.bank  # into the slot it runs from: its own bank
        else:
            if len(banks_at.get(value, ())) > 1:
                out.append(f"3. line {it.line}: banks {', '.join(str(b) for b in sorted(banks_at[value]))} all have "
                           f"code at ${value:04X}, and which one is mapped there here isn't known yet: say what you "
                           "know (correct_split) or leave it for later")
            continue
        for name in used:
            if bank_of[name] != want:
                out.append(f"3. line {it.line}: {name} is in bank {bank_of[name]}, but at ${value:04X} this routine "
                           f"reaches bank {want}")
    return out


def _data_checks(r: Routine, layout) -> list[str]:
    """4: .db and .dw only over what the map has as data in the routine, or an instruction a branch lands inside."""
    data = [(lo - r.flat, lo - r.flat + n) for lo, n, _kind in r.data]
    inner = set()
    for _it, _off, text, _ln in layout:
        kind, target = z80.flow(text)
        if target is not None and kind in ("jump", "cjump", "call") and r.cpu <= target < r.cpu + len(r.code):
            inner.add(target - r.cpu)
    out = []
    for it, off, _t, size in layout:
        if it.kind not in ("db", "dw"):
            continue
        covered = all(any(lo <= k < hi for lo, hi in data) for k in range(off, off + size))
        entered = any(off < t < off + size for t in inner)
        if not covered and not entered:
            out.append(f"4. line {it.line}: `{it.text[:40]}` writes bytes the routine runs as data: write them as "
                       "instructions (.db and .dw are for its operands, tables, or an instruction a branch lands inside)")
    return out


def _name_checks(labels, header: list[str], layout) -> list[str]:
    out = []
    for n, name in labels:
        if re.fullmatch(r"[-+]+|__", name):
            continue  # (an anonymous label: no name to give)
        if AUTO.match(name):
            out.append(f"6. line {n}: `{name}` is an automatic name: say what it is")
    said = " ".join(header)
    if len(said) < 60 or len(header) < 2:
        out.append("6. before its label, say in comment lines what the routine does, what it takes and what it returns")
    maps = any(it.kind == "insn" and (op := _operands(t, ln)) and op[0] == "write" and op[1] in (0xFFFD, 0xFFFE, 0xFFFF)
               for it, _off, t, ln in layout)
    if maps and "bank" not in said.lower():
        out.append("6. it maps a bank: the comment before it says which, or whose")
    return out


# ---------------------------------------------------------------- a draft to start from

def draft(r: Routine) -> str:
    """The routine as WLA-DX assembly that rebuilds its bytes, with what the project knows filled in: its functions'
    and symbols' names, the hardware's names, `.db` over the data the map has in it, and a child label at each branch
    target inside it. Automatic names and no comment yet: what's left for the agent to write."""
    names: dict[int, str] = {}
    for name, value in sorted(r.symbols.items()):
        names.setdefault(value, name)
    by_cpu: dict[int, list[int]] = {}
    for flat, cpu in r.runs_at.items():
        by_cpu.setdefault(cpu, []).append(flat)
    data = sorted((lo - r.flat, lo - r.flat + n) for lo, n, _k in r.data)

    def in_data(off: int):
        return next(((lo, hi) for lo, hi in data if lo <= off < hi), None)

    pieces, off, inner = [], 0, set()
    while off < len(r.code):
        span = in_data(off)
        if span:
            end = min(span[1], len(r.code))
            pieces.append((off, ".db " + ", ".join(f"${b:02x}" for b in r.code[off:end]), end - off, None, None))
            off = end
            continue
        text, ln = z80.decode(r.code[off:off + 4], r.cpu + off)
        kind, target = z80.flow(text)
        if target is not None and r.cpu <= target < r.cpu + len(r.code) and kind in ("jump", "cjump", "call"):
            inner.add(target - r.cpu)
        pieces.append((off, text, ln, kind, target))
        off += ln
    starts = {p[0] for p in pieces}

    def code_name(target: int) -> str:
        if r.cpu <= target < r.cpu + len(r.code) and target - r.cpu in starts:
            return f"@l_{target:04x}"
        if target >> 14 == r.slot and not (r.slot == 0 and target < 0x400):
            flats = [flat_of(r.bank, target)]  # (the slot it runs from: its own bank)
        elif target < BANK:
            flats = [target]  # (bank 0, which the first slot holds)
        else:
            flats = by_cpu.get(target, [])  # (another slot: the function there, when only one bank has one)
        named = [r.functions[f] for f in flats if f in r.functions]
        return named[0] if len(named) == 1 else f"${target:04x}"

    def address(value: int, role: str) -> str:
        if value in sms_hw.MAPPER:
            return sms_hw.MAPPER[value]
        if value >= 0xC000 and value in names:
            return names[value]
        return f"${value:04x}"

    lines = []
    for off, text, ln, kind, target in pieces:
        if off in inner:
            lines.append(f"@l_{r.cpu + off:04x}:")
        if kind is None or text.startswith("db "):  # (data, or a byte that's no instruction)
            lines.append("    " + ("." + text if text.startswith("db ") else text))
            continue
        out = text
        if (m := re.match(rf"^(jp|jr|call|djnz)( {_CONDITION}, | )\$([0-9a-f]{{4}})$", text)):
            name = code_name(int(m.group(3), 16))
            if name.startswith("$") and m.group(1) in ("jr", "djnz"):
                name += "-CADDR-1"  # (WLA-DX reads a number there as the offset: this is the address)
            out = f"{m.group(1)}{m.group(2)}{name}"
        elif (operand := _operands(text, ln)) is not None:
            role, value, _p = operand
            if role == "port":
                name = sms_hw.port_name(value, write=text.startswith("out"))
                out = re.sub(r"\(\$[0-9a-f]{2}\)", f"({name})", text) if name else text
            elif role in ("read", "write"):
                out = re.sub(r"\(\$[0-9a-f]{4}\)", f"({address(value, role)})", text)
            elif role == "imm16" and value >= 0x100 and value in names:
                out = re.sub(r"\$[0-9a-f]{4}$", names[value], text)
        lines.append("    " + out)
    return f"; (say what it does)\n{r.name}:\n" + "\n".join(lines) + "\n"
