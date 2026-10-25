"""Judging a hand-written C64 routine documented in 6502 assembly.

The assembler is py65, which is part of this project, so the track does not need ca65. A routine is
accepted when:

1. It assembles at its address to the snapshot's bytes.
2. Every address is a name. Assembled again with those names moved, each address operand changes.
   A branch inside the routine is a label, so its displacement does not have to change.
3. A ``jsr`` / ``jmp`` names the routine it reaches when the project has a name there.
4. ``.byte`` only where the map marked data inside the routine.
5. Hardware, the processor port and KERNAL/BASIC calls use the names from ``c64_hw`` (``sta
   VIC_BORDERCOLOR``, ``jsr CHROUT``).
6. The routine's label is not an automatic ``func_....`` name, and a comment before it says what it does.

``NAME = $number`` lines become project symbols. ``.org``, ``.include`` and macros are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import c64, c64_hw, mos6502
from .gb_asm import AsmError, Verdict

MAX_TEXT = 64 * 1024
_IDENT = r"[A-Za-z_]\w*"
_LABEL = re.compile(rf"^\s*({_IDENT})\s*:")
_NUMBER = r"(?:\$[0-9A-Fa-f]+|%[01]+|\d+)"
_DEFINE = re.compile(rf"^\s*({_IDENT})\s*=\s*({_NUMBER})\s*$")
_DATA = re.compile(r"^\s*\.(byte|byt|word)\b", re.I)
AUTO = re.compile(r"^@?(?:func|sub|loc|label|lbl|l|unk|unknown)_[0-9A-Fa-f_]+$", re.I)
_MNEMONICS = frozenset({"adc", "and", "asl", "bcc", "bcs", "beq", "bit", "bmi", "bne", "bpl", "brk", "bvc", "bvs",
                        "clc", "cld", "cli", "clv", "cmp", "cpx", "cpy", "dec", "dex", "dey", "eor", "inc", "inx",
                        "iny", "jmp", "jsr", "lda", "ldx", "ldy", "lsr", "nop", "ora", "pha", "php", "pla", "plp",
                        "rol", "ror", "rti", "rts", "sbc", "sec", "sed", "sei", "sta", "stx", "sty", "tax", "tay",
                        "tsx", "txa", "txs", "tya", "a", "x", "y"})
_INSTRUCTIONS = _MNEMONICS - {"a", "x", "y"}


@dataclass
class Routine:
    name: str
    cpu: int
    code: bytes
    symbols: dict[str, int]
    flat: int = 0
    data: list[tuple[int, int, str]] = field(default_factory=list)
    #: the project's routines by address: where the program has its own code (in RAM under a switched-out ROM,
    #: say), its routine's name is the one, not the ROM's
    own: dict[int, str] = field(default_factory=dict)
    #: stretches of the program no routine and no data holds, (start, end), near this routine's branches
    free: list[tuple[int, int]] = field(default_factory=list)
    #: how many calls, jumps and branches elsewhere in the program reach this routine's start (None: not known)
    callers: int | None = None
    #: other routines with exactly these bytes, by address
    twins: list[int] = field(default_factory=list)


def available() -> bool:
    try:
        import py65.assembler  # noqa: F401
        return True
    except ImportError:
        return False


def _number(text: str) -> int:
    t = text.strip()
    return int(t[1:], 16) if t.startswith("$") else int(t[1:], 2) if t.startswith("%") else int(t)


def _render(value: int, *, zp: bool) -> str:
    value &= 0xFFFF
    if zp and value < 0x100:
        return f"${value:02X}"
    return f"${value:04X}"


def parse(text: str) -> tuple[list[tuple[int, str, str]], list[tuple[int, str]], list[str], dict[str, int], list[str]]:
    """(statements as (line, kind, text), labels, header comments, defines, problems)."""
    if len(text) > MAX_TEXT:
        raise AsmError(f"the routine is over {MAX_TEXT // 1024} KB of text")
    items, labels, header, defines, problems = [], [], [], {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        code, _sep, comment = raw.partition(";")
        if not code.strip():
            if comment.strip() and not labels:
                header.append(comment.strip())
            continue
        if (m := _DEFINE.match(code)):
            defines[m.group(1)] = _number(m.group(2))
            items.append((n, "def", code.strip()))
            continue
        while (m := _LABEL.match(code)):
            labels.append((n, m.group(1)))
            code = code[m.end():]
        stmt = code.strip()
        if not stmt:
            continue
        if (m := re.match(r"^([^\s:;]+)\s*:", stmt)) and not re.fullmatch(_IDENT, m.group(1)):
            problems.append(f"line {n}: `{m.group(1)}` isn't a name: a label starts with a letter or _ "
                            f"(`{'target_' + h.group(0) if (h := re.search(r'[0-9A-Fa-f]{4}', m.group(1))) else 'target'}:`)")
            continue
        word = stmt.split()[0]
        if not stmt.startswith(".") and word.lower() not in _INSTRUCTIONS:
            if re.fullmatch(_IDENT, stmt):
                problems.append(f"line {n}: `{stmt}` on its own isn't an instruction: a name goes after one "
                                f"(`lda {stmt}`), and a label takes a colon (`{stmt}:`)")
            else:
                problems.append(f"line {n}: `{word}` isn't a 6502 instruction")
            continue
        if stmt.startswith(".") and not _DATA.match(stmt):
            problems.append(f"line {n}: `{stmt.split()[0]}` isn't taken here: instructions, labels, .byte and "
                            "NAME = $number only")
            continue
        kind = "db" if _DATA.match(stmt) else "insn"
        items.append((n, kind, stmt))
    if not any(k != "def" for _n, k, _t in items):
        problems.append("there's no code in it")
    return items, labels, header, defines, problems


def contained(text: str) -> bool:
    try:
        return not any("isn't taken here" in p for p in parse(text)[4])
    except AsmError:
        return False


_OFFSET = re.compile(rf"\s*([+-])\s*({_NUMBER})")


def _substitute(line: str, mapping: dict[str, int], zp: set[str]) -> str:
    """Names become numbers. ``NAME+N`` / ``NAME-N`` (a table's second byte, a patched instruction's operand) is
    worked out here, since py65 takes numbers only."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in mapping or name.lower() in _MNEMONICS:
            return m.group(0)
        value, rest = mapping[name], m.group(2) or ""
        for sign, number in _OFFSET.findall(rest):
            value += _number(number) if sign == "+" else -_number(number)
        return _render(value, zp=name in zp)

    return re.sub(rf"\b({_IDENT})\b((?:{_OFFSET.pattern})*)", repl, line)


def _data_bytes(text: str) -> bytes:
    """``.byte`` is one byte each; ``.word`` is little-endian."""
    wide = text.lower().lstrip().startswith(".word")
    body = text.split(None, 1)[1] if len(text.split(None, 1)) == 2 else ""
    out = bytearray()
    for part in body.split(","):
        if not part.strip():
            continue
        value = _number(part)
        if wide:
            out.extend((value & 0xFF, (value >> 8) & 0xFF))
        elif not 0 <= value <= 0xFF:
            raise ValueError(part)
        else:
            out.append(value)
    return bytes(out)


def _assemble(items, label_at: dict[int, list[str]], mapping: dict[str, int], origin: int, *,
              want: bytes | None = None, wide: set[int] | None = None) -> tuple[bytes, list[tuple]]:
    """Iteratively place labels and assemble. ``label_at`` maps a statement's line number to labels on it.

    A forward label is unknown until the pass that reaches it, so each pass substitutes from the
    addresses the previous pass recorded and only falls back to a placeholder while a name is still
    missing.

    A name under $100 can be zero page or absolute. With ``want`` (the snapshot's bytes) a line takes
    the absolute form when that is what the snapshot has there, and its line number goes into
    ``wide``; the moved passes then pass the same ``wide`` so each line keeps its form.
    """
    zp = {name for name, value in mapping.items() if 0 <= value < 0x100}
    if wide is None:
        wide = set()
    places: dict[str, int] = {}
    rows: list[tuple] = []
    for _pass in range(8):
        pc, rows = origin, []
        if want is not None:
            wide.clear()  # decided again each pass, since early passes may hold placeholders
        fresh: dict[str, int] = {}
        pending: list[str] = []
        for line_no, kind, text in items:
            pending.extend(label_at.get(line_no, []))
            for name in pending:
                fresh[name] = pc
            pending = []
            if kind == "def":
                continue
            if kind == "db":
                try:
                    blob = _data_bytes(text)
                except ValueError as e:
                    raise AsmError(f"line {line_no}: invalid .byte") from e
            else:
                names = {**mapping, **places, **fresh}
                substituted = _substitute(text, names, set() if line_no in wide else zp)
                try:
                    blob = c64.assemble(substituted, pc)
                except Exception as e:
                    if _pass < 7 and re.search(rf"\b{_IDENT}\b", substituted):
                        blob = b"\xea"
                    else:
                        raise AsmError(f"line {line_no}: {e}") from e
                if want is not None and line_no not in wide:
                    at = pc - origin
                    if blob != want[at:at + len(blob)] and (absolute := _substitute(text, names, set())) != substituted:
                        try:
                            other = c64.assemble(absolute, pc)
                        except Exception:
                            other = None
                        if other is not None and other == want[at:at + len(other)]:
                            blob = other
                            wide.add(line_no)
            rows.append((line_no, pc - origin, text, blob))
            pc += len(blob)
        if pending:
            for name in pending:
                fresh[name] = pc
        places = fresh
    blob = b"".join(row[3] for row in rows)
    return blob, rows


def _moved(value: int) -> int:
    if value < 0x100:
        return (value + 0x10) & 0xFF
    if 0xD000 <= value <= 0xDFFF:
        return value  # hardware is the other pass
    return (value + 0x10) & 0xFFFF


def _hw_moved(value: int) -> int:
    if c64_hw.name_of(value) or value in c64_hw.ALIASES or c64_hw.name_of(value, call=True):
        return (value + 0x10) & 0xFFFF
    return value


def _switched_out(cpu: int) -> tuple[tuple[int, int], ...]:
    """The ROM areas a routine at ``cpu`` shows are switched out: its own code is in RAM there. Under BASIC
    ($A000-$BFFF), BASIC is out; under the KERNAL ($E000-$FFFF), the KERNAL is, and on the C64 that takes BASIC out
    with it (no processor port setting has the KERNAL out and BASIC in)."""
    basic, kernal = (0xA000, 0xC000), (0xE000, 0x10000)
    if kernal[0] <= cpu < kernal[1]:
        return basic, kernal
    if basic[0] <= cpu < basic[1]:
        return (basic,)
    return ()


def judge(r: Routine, text: str, tmp_root=None) -> Verdict:
    items, labels, header, defines, problems = parse(text)
    globals_ = [name for _n, name in labels if re.fullmatch(_IDENT, name)]
    label = globals_[0] if globals_ else ""
    if problems:
        return Verdict(False, 0, 0, problems, label)
    if not label:
        return Verdict(False, 0, 0, ["give the routine its label before its first instruction"], label)
    hw = c64_hw.hardware_names()
    own = set(globals_) | set(defines)
    if clash := sorted(own & set(hw)):
        return Verdict(False, 0, 0, [f"{', '.join(clash)} already names hardware: pick another name"], label)
    label_at: dict[int, list[str]] = {}
    stmt_lines = [n for n, kind, _text in items if kind != "def"]
    for line, name in labels:
        host = next((n for n in stmt_lines if n >= line), stmt_lines[-1] if stmt_lines else line)
        label_at.setdefault(host, []).append(name)
    mapping = {n: v for n, v in {**r.symbols, **hw, **defines}.items() if n not in own or n in defines}
    # the routine's own name is its entry, not a free symbol
    mapping.pop(r.name, None)
    wide: set[int] = set()
    try:
        got, rows = _assemble(items, label_at, mapping, r.cpu, want=r.code, wide=wide)
    except AsmError as e:
        return Verdict(False, 0, 0, [f"1. it doesn't assemble: {e}"], label)
    same = next((k for k, (a, b) in enumerate(zip(got, r.code)) if a != b), min(len(got), len(r.code)))
    if got != r.code:
        at_row = {row[0]: r.cpu + row[1] for row in rows}
        places = {name: at_row[line] for line, names in label_at.items() if line in at_row for name in names}
        return Verdict(False, same, len(got), [
            f"1. the bytes differ ({same} of {len(r.code)} match, assembled {len(got)})"]
            + _first_difference(r, rows, same, places) + gap_notes(r), label)
    # two passes: addresses moved, and hardware moved, so a raw number fails exactly one of them
    addr_map = {n: (v if n in hw else _moved(v)) for n, v in mapping.items()}
    hw_map = {n: (_hw_moved(v) if (n in hw or c64_hw.name_of(v)) else v) for n, v in mapping.items()}
    try:
        shifted, _rows = _assemble(items, label_at, addr_map, r.cpu, wide=set(wide))
        hw_shifted, _hrows = _assemble(items, label_at, hw_map, r.cpu, wide=set(wide))
    except AsmError as e:
        return Verdict(False, len(r.code), len(got), [f"2. it doesn't assemble moved: {e}"], label)
    problems += _operand_checks(r, rows, got, shifted, hw_shifted)
    problems += _name_checks(label, header, globals_)
    if problems:
        problems += gap_notes(r)
    kept = "\n".join(line for line in text.splitlines() if not _DEFINE.match(line.split(";")[0]))
    return Verdict(not problems, len(r.code), len(got), problems, label, defines, kept.strip("\n") + "\n")


def _first_difference(r: Routine, rows, same: int, places: dict[str, int] | None = None) -> list[str]:
    """Which line the bytes first part at, what it became and what the snapshot has there. When it's a branch or
    jump to a label in the wrong place, where the label is and where it has to go (or that the target is outside the
    routine, where only `NAME = $addr` reaches)."""
    places = places or {}
    end = r.cpu + len(r.code)
    for line_no, off, text, blob in rows:
        if off <= same < off + max(len(blob), 1) and off < len(r.code):
            mine, mine_len = mos6502.decode(blob[:3], r.cpu + off) if blob else ("nothing", 0)
            theirs, theirs_len = mos6502.decode(r.code[off:off + 3], r.cpu + off)
            note = (f"line {line_no}: `{text}` assembles to `{mine}` ({mine_len} bytes), but the snapshot has "
                    f"`{theirs}` ({theirs_len} bytes) here")
            if mine_len != theirs_len and mine.split()[0] == theirs.split()[0]:
                note += (": write the address with four digits for the absolute form, two for zero page"
                         if "$" in text else "")
            _kind, target = mos6502.flow(theirs)
            operand = text.split(None, 1)[1] if len(text.split(None, 1)) == 2 else ""
            label = next((w for w in re.findall(rf"\b{_IDENT}\b", operand) if w in places), None)
            if target is not None and label is not None and places[label] != target:
                if not r.cpu <= target < end:
                    note += (f". `{label}` is a label in this routine (at ${places[label]:04X}), but ${target:04X} is "
                             f"outside it (it runs ${r.cpu:04X}-${end - 1:04X}). A label only marks an instruction "
                             f"you write here, so define the name instead: `{label} = ${target:04X}`")
                else:
                    there = next((row for row in rows if r.cpu + row[1] == target), None)
                    note += (f". `{label}` sits at ${places[label]:04X}; ${target:04X} is "
                             + (f"line {there[0]} (`{there[2]}`): put the label on that line" if there else
                                "inside another instruction as you've written it"))
            return [note]
    return []


def gap_notes(r: Routine) -> list[str]:
    """Branches and jumps out of the routine into bytes no routine or data holds: the split probably cut the routine
    short (bytes just before it), or missed one. The agent fixes it with create_function / merge_function."""
    out, seen = [], set()
    off = 0
    while off < len(r.code):
        text, ln = mos6502.decode(r.code[off:off + 3], r.cpu + off)
        _kind, target = mos6502.flow(text)
        for lo, hi in r.free:
            if target is not None and lo <= target < hi and (lo, hi) not in seen:
                seen.add((lo, hi))
                note = (f"`{text}` at ${r.cpu + off:04X} goes to ${target:04X}, in ${lo:04X}-${hi - 1:04X}: bytes no "
                        "routine holds. ")
                if hi == r.cpu:
                    note += (f"This routine probably starts at ${lo:04X} and the split cut it short: "
                             f"create_function(at=\"${lo:04X}\") makes those bytes a routine, then "
                             f"merge_function(\"{r.name}\") joins the two, and you document it whole. (Until then, "
                             f"`NAME = ${target:04X}` names the target.)")
                else:
                    note += (f"They're probably a routine the split missed: create_function(at=\"${lo:04X}\") "
                             f"adds it. (`NAME = ${target:04X}` names the target meanwhile.)")
                out.append(note)
        off += max(ln, 1)
    return out


DATA_SIGNS = 2  # how many signs make data_notes speak up: one alone is common enough in real code


def data_notes(r: Routine) -> list[str]:
    """Signs that the bytes the finder took for a routine are data (graphics, tables, text), not code. The finder is
    generous on purpose, so some of what it marks is data; the agent decides, and delete_function is how it says so.
    Empty unless at least DATA_SIGNS of them show up together."""
    ins, off = [], 0
    while off < len(r.code):
        text, ln = mos6502.decode(r.code[off:off + 3], r.cpu + off)
        ins.append((r.cpu + off, text, ln))
        off += max(ln, 1)
    if not ins:
        return []
    signs = []
    bad = [(a, t) for a, t, _ln in ins if t.startswith("db ")]
    if bad:
        signs.append(f"${bad[0][0]:04X} isn't an instruction at all (`{bad[0][1]}`: no 6502 opcode)")
    for (a, t, ln), (_b, u, _l) in zip(ins, ins[1:]):
        if t == u and ln > 1 and not t.startswith(("b", "jmp", "jsr")):
            signs.append(f"`{t}` at ${a:04X} comes twice in a row: the same instruction on the same address does "
                         "nothing useful a second time, while a byte pattern repeats like that")
            break
    for a, t, ln in ins:
        m = re.search(r"\$([0-9a-f]{2})([0-9a-f]{2})\b", t)
        if ln == 3 and m and m.group(1) == m.group(2):
            signs.append(f"`{t}` at ${a:04X} uses ${m.group(1).upper() * 2}, an address made of one byte twice, "
                         "as filled-in graphics or a table's repeating bytes read as code do")
            break
    last = ins[-1][1].split()[0]
    if last not in ("rts", "rti", "jmp", "brk"):
        signs.append(f"it ends on `{ins[-1][1]}` at ${ins[-1][0]:04X} with no rts, rti or jmp, so it runs on into "
                     "whatever comes next (fine only if that's the next routine on purpose)")
    if r.callers == 0:
        signs.append("no routine calls, jumps or branches to it (an interrupt handler or a routine in a table of "
                     "addresses is reached another way: look for the code that stores its address)")
    if r.twins:
        signs.append("exactly the same bytes are also at " + ", ".join(f"${t:04X}" for t in r.twins[:4]) +
                     ": a repeated table or graphic is more likely than the same routine twice")
    return signs if len(signs) >= DATA_SIGNS else []


def _operand_checks(r: Routine, rows, got: bytes, shifted: bytes, hw_shifted: bytes) -> list[str]:
    out = []
    data = [(lo - r.flat, lo - r.flat + n) for lo, n, _k in r.data]
    for line_no, off, text, blob in rows:
        if text.lower().startswith(".byte") or text.lower().startswith(".word") or text.lower().startswith(".byt"):
            if not any(lo <= off < hi for lo, hi in data):
                out.append(f"4. line {line_no}: .byte only where the map has data in this routine")
            continue
        raw = got[off:off + len(blob)]
        if not raw:
            continue
        decoded, ln = mos6502.decode(raw, r.cpu + off)
        kind, target = mos6502.flow(decoded)
        if target is None:
            mem = _abs(decoded)
            target = mem
            role = "mem" if mem is not None else None
        else:
            role = kind
        if target is None or role is None:
            continue
        if role == "cjump" and r.cpu <= target < r.cpu + len(r.code):
            continue
        if role == "mem" and r.cpu <= target < r.cpu + len(r.code) and _named(text):
            continue  # its own bytes by a label (`sta patch+1`: self-modifying code): a label doesn't move
        hw = c64_hw.name_of(target) or (c64_hw.name_of(target, call=True) if role in ("call", "jump") else None)
        if role in ("call", "jump") and not c64_hw.name_of(target) and (
                target in r.own or any(lo <= target < hi for lo, hi in _switched_out(r.cpu))):
            hw = None  # the program's own routine there, or RAM while the ROM is switched out: not the ROM's
        same_addr = shifted[off:off + ln] == got[off:off + ln]
        same_hw = hw_shifted[off:off + ln] == got[off:off + ln]
        if hw and same_hw:
            out.append(f"5. line {line_no}: `{text}` uses ${target:04X}, which is {hw}")
        elif not hw and same_addr and same_hw and role in ("call", "jump", "mem"):  # (a ROM name moves in the other)
            out.append(f"2. line {line_no}: `{text}` leaves ${target:04X} as a number; name it")
    return out


def _named(text: str) -> bool:
    """Whether an instruction's operand is written with a name rather than a number."""
    parts = text.split(None, 1)
    operand = parts[1] if len(parts) == 2 else ""
    return any(w.lower() not in _MNEMONICS for w in re.findall(rf"\b{_IDENT}\b", operand.split(";")[0]))


def _abs(text: str) -> int | None:
    if "#" in text.split(",")[0]:
        return None
    m = re.search(r"\$([0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})\b", text)
    return int(m.group(1), 16) if m else None


def _name_checks(label: str, header: list[str], labels: list[str]) -> list[str]:
    out = []
    if AUTO.match(label) or re.fullmatch(r"func_[0-9A-Fa-f]{4}", label):
        out.append(f"6. `{label}` is an automatic name: write a real one as the label, and it becomes the routine's "
                   "name when it's documented (no rename_symbol needed)")
    if not header:
        out.append("6. a comment before the routine should say what it does")
    for name in labels[1:]:
        if AUTO.match(name):
            out.append(f"6. `{name}` is an automatic local name")
    return out


def draft(r: Routine) -> str:
    """A listing that rebuilds the bytes, with hardware and known symbols filled in. The name and the
    comment are still the agent's."""
    names = dict(r.own)  # the project's own routines first, over a ROM name at the same address
    for name, value in r.symbols.items():
        names.setdefault(value, name)
    lines = [f"; {r.name}"]
    lines.append(f"{r.name}:")
    off = 0
    code = r.code
    while off < len(code):
        text, ln = mos6502.decode(code[off:off + 3], r.cpu + off)
        kind, target = mos6502.flow(text)
        call = kind in ("call", "jump")
        if target is not None and (name := names.get(target) or c64_hw.name_of(target, call=call)):
            text = re.sub(r"\$[0-9A-Fa-f]+\b", name, text, count=1)
        else:
            mem = _abs(text)
            if mem is not None and (name := c64_hw.name_of(mem, call=False)):
                text = re.sub(r"\$[0-9A-Fa-f]+\b", name, text, count=1)
        lines.append(f"  {text}")
        off += ln
    return "\n".join(lines) + "\n"

