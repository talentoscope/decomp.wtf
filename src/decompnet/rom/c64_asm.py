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


@dataclass
class Routine:
    name: str
    cpu: int
    code: bytes
    symbols: dict[str, int]
    flat: int = 0
    data: list[tuple[int, int, str]] = field(default_factory=list)


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


def _substitute(line: str, mapping: dict[str, int], zp: set[str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(0)
        if name not in mapping or name.lower() in _MNEMONICS:
            return name
        return _render(mapping[name], zp=name in zp)

    return re.sub(rf"\b{_IDENT}\b", repl, line)


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


def _assemble(items, label_at: dict[int, list[str]], mapping: dict[str, int], origin: int) -> tuple[bytes, list[tuple]]:
    """Iteratively place labels and assemble. ``label_at`` maps a statement's line number to labels on it.

    A forward label is unknown until the pass that reaches it, so each pass substitutes from the
    addresses the previous pass recorded and only falls back to a placeholder while a name is still
    missing.
    """
    zp = {name for name, value in mapping.items() if 0 <= value < 0x100}
    places: dict[str, int] = {}
    rows: list[tuple] = []
    for _pass in range(8):
        pc, rows = origin, []
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
                substituted = _substitute(text, {**mapping, **places, **fresh}, zp)
                try:
                    blob = c64.assemble(substituted, pc)
                except Exception as e:
                    if _pass < 7 and re.search(rf"\b{_IDENT}\b", substituted):
                        blob = b"\xea"
                    else:
                        raise AsmError(f"line {line_no}: {e}") from e
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
    if c64_hw.name_of(value) or value in c64_hw.ALIASES:
        return (value + 0x10) & 0xFFFF
    return value


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
    try:
        got, rows = _assemble(items, label_at, mapping, r.cpu)
    except AsmError as e:
        return Verdict(False, 0, 0, [f"1. it doesn't assemble: {e}"], label)
    same = next((k for k, (a, b) in enumerate(zip(got, r.code)) if a != b), min(len(got), len(r.code)))
    if got != r.code:
        return Verdict(False, same, len(got), [
            f"1. the bytes differ ({same} of {len(r.code)} match, assembled {len(got)})"], label)
    # two passes: addresses moved, and hardware moved, so a raw number fails exactly one of them
    addr_map = {n: (v if n in hw else _moved(v)) for n, v in mapping.items()}
    hw_map = {n: (_hw_moved(v) if (n in hw or c64_hw.name_of(v)) else v) for n, v in mapping.items()}
    try:
        shifted, _rows = _assemble(items, label_at, addr_map, r.cpu)
        hw_shifted, _hrows = _assemble(items, label_at, hw_map, r.cpu)
    except AsmError as e:
        return Verdict(False, len(r.code), len(got), [f"2. it doesn't assemble moved: {e}"], label)
    problems += _operand_checks(r, rows, got, shifted, hw_shifted)
    problems += _name_checks(label, header, globals_)
    kept = "\n".join(line for line in text.splitlines() if not _DEFINE.match(line.split(";")[0]))
    return Verdict(not problems, len(r.code), len(got), problems, label, defines, kept.strip("\n") + "\n")


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
        hw = c64_hw.name_of(target) or (c64_hw.name_of(target, call=True) if role in ("call", "jump") else None)
        same_addr = shifted[off:off + ln] == got[off:off + ln]
        same_hw = hw_shifted[off:off + ln] == got[off:off + ln]
        if hw and same_hw:
            out.append(f"5. line {line_no}: `{text}` uses ${target:04X}, which is {hw}")
        elif not hw and same_addr and role in ("call", "jump", "mem"):
            out.append(f"2. line {line_no}: `{text}` leaves ${target:04X} as a number; name it")
    return out


def _abs(text: str) -> int | None:
    if "#" in text.split(",")[0]:
        return None
    m = re.search(r"\$([0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})\b", text)
    return int(m.group(1), 16) if m else None


def _name_checks(label: str, header: list[str], labels: list[str]) -> list[str]:
    out = []
    if AUTO.match(label) or re.fullmatch(r"func_[0-9A-Fa-f]{4}", label):
        out.append(f"6. `{label}` is an automatic name: say what the routine does")
    if not header:
        out.append("6. a comment before the routine should say what it does")
    for name in labels[1:]:
        if AUTO.match(name):
            out.append(f"6. `{name}` is an automatic local name")
    return out


def draft(r: Routine) -> str:
    """A listing that rebuilds the bytes, with hardware and known symbols filled in. The name and the
    comment are still the agent's."""
    names = {}
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

