"""Sega Master System (.sms) and Game Gear (.gg) ROMs: detection, header, banking and function discovery, on top of
the fork's own Z80 disassembler (see `z80.py` for why one is needed).

What it covers: reading, disassembly and function discovery, and with the pinned GBDK-2020 toolchain installed the
matching lane (`gb_build` with `arch="z80"`: SDCC's Z80 back end, byte-compared against the ROM).

Detection. A cartridge made by Sega carries a header at 0x7FF0 (0x3FF0 or 0x1FF0 in a ROM of 16 or 8 KiB) that
starts with `TMR SEGA`; its last byte names the region (3 and 4 are Master System, 5 to 7 Game Gear) and the size.
Plenty of ROMs have no header at all (early Japanese games, much homebrew), so a file named `.sms` or `.gg` with a
plausible size is accepted too. Dumps with a 512-byte copier header in front are stripped of it.

Banking. The Sega mapper has three 16 KiB slots: slot 0 (0x0000) shows bank 0, slot 1 (0x4000) and slot 2 (0x8000)
show any bank, set by writes to 0xFFFE and 0xFFFF. Which bank is mapped when is in the code, not in the ROM's layout:
discovery follows the code with the banks it maps (`sms_trace`), so each function runs at the address its bank is
mapped to when it's called, and a bank no code was followed into is placed where the game pages its banks. The
power-on model (bank 0 at 0x0000, bank 1 at 0x4000, bank 2 at 0x8000 in a ROM of three banks or fewer; in a bigger
ROM every other bank at 0x4000) is what's left when nothing says otherwise. A function's name spells its bank and
the CPU address it runs at (`func_<bank>_<address>`), because two banks can hold code at the same address.
"""

# Portions ported from SynapticDreams; Copyright (c) 2026 Sam Wolfs; MIT License (see licenses/synaptic-sms-MIT.txt).
from __future__ import annotations

import bisect
import hashlib
import re
from pathlib import Path

from . import gb, sms_trace, z80
from .n64 import RomError

BANK_SIZE = 0x4000
MAGIC = b"TMR SEGA"
HEADER_OFFSETS = (0x7FF0, 0x3FF0, 0x1FF0)
COPIER_HEADER = 0x200
MAX_SIZE = 0x400000  # the mapper addresses 4 MiB
REGIONS = {3: "SMS Japan", 4: "SMS Export", 5: "GG Japan", 6: "GG Export", 7: "GG International"}
SIZES = {0xA: 0x2000, 0xB: 0x4000, 0xC: 0x8000, 0xD: 0xC000, 0xE: 0x10000, 0xF: 0x20000,
         0x0: 0x40000, 0x1: 0x80000, 0x2: 0x100000}
PLATFORMS = {"sms": "Sega Master System", "gg": "Sega Game Gear"}
#: Where the CPU starts looking: the reset vector, the RST handlers (0x38 is also the interrupt), and the NMI.
VECTORS = tuple(range(0x00, 0x40, 0x08)) + (0x66,)
MAX_FUNCTION = 0x800  # a function longer than this is more likely a run of code the sweep never ended


def strip_copier_header(data: bytes) -> bytes:
    """Dumps from old copiers start with 512 bytes that are not part of the ROM."""
    if len(data) % BANK_SIZE == COPIER_HEADER and header_offset(data[COPIER_HEADER:]) is not None:
        return data[COPIER_HEADER:]
    return data


def header_offset(data: bytes) -> int | None:
    """Where the `TMR SEGA` header sits in this ROM, or None. The size decides the place: 0x7FF0 from 32 KiB up,
    0x3FF0 in a ROM of 16 KiB, 0x1FF0 in one of 8."""
    off = HEADER_OFFSETS[0] if len(data) >= 0x8000 else HEADER_OFFSETS[1] if len(data) >= 0x4000 else HEADER_OFFSETS[2]
    return off if len(data) >= off + 0x10 and data[off:off + 8] == MAGIC else None


def _size_ok(size: int) -> bool:
    return 0x2000 <= size <= MAX_SIZE and size % 0x2000 == 0


def platform_of(data: bytes, name: str = "") -> str | None:
    """"sms", "gg", or None when this is not one of their ROMs."""
    off = header_offset(data)
    if off is not None and _size_ok(len(data)):
        region = data[off + 0xF] >> 4
        if region in REGIONS:
            return "gg" if region >= 5 else "sms"
    ext = Path(name).suffix.lower()
    if ext in (".sms", ".gg") and _size_ok(len(data)):
        return ext[1:]
    return None


def is_sms(data: bytes, name: str = "") -> bool:
    return platform_of(strip_copier_header(data), name) is not None


def checksum(data: bytes, off: int) -> int:
    """The header's checksum: the sum of every byte below the 16-byte header, and of those after it up to the declared
    size (the header itself is left out), modulo 0x10000."""
    declared = SIZES.get(data[off + 0xF] & 0xF, len(data))
    size = min(declared, len(data))
    return (sum(data[:off]) + sum(data[off + 0x10:size])) & 0xFFFF


def _product(data: bytes, off: int) -> str:
    code = f"{data[off + 0xE] >> 4:x}{data[off + 0xD]:02x}{data[off + 0xC]:02x}"
    return code.lstrip("0") or "0" if code.isdigit() else ""  # a BCD number: anything else is not a product code


def read_info(data: bytes, name: str = "") -> dict:
    """The cartridge header, in the shape the rest of the fork expects from a ROM reader."""
    data = strip_copier_header(data)
    platform = platform_of(data, name)
    if platform is None:
        raise RomError("this isn't a Master System or Game Gear ROM")
    off = header_offset(data)
    stem = Path(name).stem if name else ""
    banks = -(-len(data) // BANK_SIZE)
    info = {
        "title": stem or PLATFORMS[platform] + " ROM",
        "game_code": "",
        "region": "unknown",
        "version": 0,
        "size": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
        "entrypoint": 0,
        "platform": platform,
        "byte_order": "little",
        "compiler_guess": "SDCC or a hand-written assembler (unknown)",
        "gp": 0,
        "header": off is not None,
        "cartridge": f"Sega mapper, {banks} bank{'s' if banks != 1 else ''}",
        "rom_banks": banks,
    }
    if off is not None:
        region = data[off + 0xF] >> 4
        declared = SIZES.get(data[off + 0xF] & 0xF)
        info.update({
            "game_code": _product(data, off),
            "region": REGIONS.get(region, f"unknown ({region})"),
            "version": data[off + 0xE] & 0xF,
            "header_offset": off,
            "rom_bytes_declared": declared,
            "size_mismatch": bool(declared and declared != len(data)),
            "checksum": f"0x{data[off + 0xA] | (data[off + 0xB] << 8):04x}",
            "checksum_ok": checksum(data, off) == (data[off + 0xA] | (data[off + 0xB] << 8)),
        })
    return info


# ---------------------------------------------------------------- banking

def bank_of(flat: int) -> int:
    return flat // BANK_SIZE


def slot_base(bank: int, size: int) -> int:
    """The CPU address where `bank` is shown: see the module docstring for the model."""
    if bank == 0:
        return 0x0000
    if bank == 2 and size <= 3 * BANK_SIZE:
        return 0x8000
    return 0x4000


def cpu_address(flat: int, size: int) -> int:
    return slot_base(bank_of(flat), size) + flat % BANK_SIZE


def function_name(flat: int, size: int, cpu: int | None = None) -> str:
    """`func_<bank>_<address>`: the address the code runs at (`cpu`, as discovery found it), else the power-on one."""
    return f"func_{bank_of(flat):03d}_{(cpu_address(flat, size) if cpu is None else cpu):04x}"


def segment_vram(bank: int, size: int) -> int:
    return slot_base(bank, size)


def target_flat(cpu: int, from_bank: int, size: int, here: int | None = None) -> int | None:
    """The flat ROM offset a call or jump to `cpu` reaches from code in `from_bank`, or None for RAM and ports.

    Into the slot the code runs from (`here`, its own CPU address, when known), its own bank. Otherwise slot 0 is
    always bank 0; slot 1 is the caller's own bank when that bank is shown there, else bank 1 (the power-on
    mapping); slot 2 is bank 2, or the caller's own bank when it is shown there."""
    if cpu < 0x4000:
        return cpu
    if cpu >= 0xC000:
        return None
    if here is not None and here >= 0x4000 and here >> 14 == cpu >> 14:
        return from_bank * BANK_SIZE + (cpu & (BANK_SIZE - 1))
    if cpu < 0x8000:
        bank = from_bank if slot_base(from_bank, size) == 0x4000 else 1
        return bank * BANK_SIZE + (cpu - 0x4000)
    bank = from_bank if slot_base(from_bank, size) == 0x8000 else 2
    return bank * BANK_SIZE + (cpu - 0x8000)


def banks(data: bytes) -> list[bytes]:
    return [data[i:i + BANK_SIZE].ljust(BANK_SIZE, b"\0") for i in range(0, len(data), BANK_SIZE)]


# ---------------------------------------------------------------- function discovery

_LD_PAIR = re.compile(r"^ld (hl|de|bc), \$([0-9a-f]{4})$")
_PAIR_WRITES = (("hl", sms_trace._WRITES_HL), ("de", sms_trace._WRITES_DE), ("bc", sms_trace._WRITES_BC))


def _walk(data: bytes, start: int, cpu: int, hard: int, starts: set[int], inline: dict[int, int] | None = None):
    """Walk the function that begins at flat offset `start`, running at CPU address `cpu`: yield (flat, text,
    length) per instruction.

    It ends at an unconditional return or jump once every forward branch seen so far has been passed, so a `ret nc`
    in the middle, the `ret` before an `else` branch's target or a loop's back-edge does not end it. An unconditional
    jump is part of the function only when something before it already branches past it (the `else` of an
    if/else); otherwise nothing after it can be reached from here and it leaves the function: a tail call, or the
    reset vector's `jp init`. A computed call (`ld de,.next / push de / jp (hl)`) comes back to the address it
    pushed, so the function goes on there. It never runs past `hard`, `MAX_FUNCTION`, the end of its bank or of the
    ROM. `inline` ({flat: count}): routines that read `count` bytes after their call; those bytes come as one
    (flat, "db", count)."""
    size = len(data)
    bank = bank_of(start)
    limit = start
    off = start
    end = min(hard, start + MAX_FUNCTION, size, (bank + 1) * BANK_SIZE)
    loaded: dict[str, int] = {}  # register pairs loaded with a constant, and what was pushed of them
    pushed = None
    while off < end:
        here = cpu + off - start
        text, ln = z80.decode(data[off:off + 4], here)
        yield off, text, ln
        nxt = off + max(1, ln)
        kind, target = z80.flow(text)
        if (m := _LD_PAIR.match(text)):
            loaded[m.group(1)] = int(m.group(2), 16)
        elif text.startswith("push "):
            pushed = loaded.get(text[5:])
        elif text in ("jp (hl)", "jp (ix)", "jp (iy)") and pushed is not None:
            back = target_flat(pushed, bank, size, here)
            if back is not None and off < back < end and back not in starts:
                limit = max(limit, back)
            pushed = None
        else:
            for pair, writes in _PAIR_WRITES:
                if writes.match(text):
                    loaded.pop(pair, None)
        if kind in ("ret", "jump") and nxt > limit:
            return
        if inline and kind == "call" and "," not in text and (n := inline.get(target_flat(target, bank, size, here))):
            if nxt + n <= end:
                yield nxt, "db", n
            nxt += n
        if target is not None and kind in ("jump", "cjump"):
            flat = target_flat(target, bank, size, here)
            if flat is not None and flat > off and flat not in starts and flat < end:
                limit = max(limit, flat)
        off = nxt


def _extent(data: bytes, start: int, cpu: int, hard: int, starts: set[int], inline: dict | None = None) -> bytes:
    """A function's bytes: from its start up to and including the instruction that ends it, never past `hard`.

    Exact extents matter for matching: a candidate compiled from C produces the function and nothing else."""
    end = start
    for off, _text, ln in _walk(data, start, cpu, hard, starts, inline):
        end = off + max(1, ln)
    return data[start:end]


def _clean(data: bytes, start: int, cpu: int, code: bytes) -> bool:
    """A real function decodes without undefined opcodes and ends on an instruction that leaves it."""
    if not code or code[:2] in (b"\x00\x00", b"\xff\xff"):
        return False
    last = None
    for _a, text, _ln in z80.disassemble(code, cpu):
        if text.startswith("db "):
            return False
        last = text
    return last is not None and z80.is_return(last)


_CALLS = (0xCD, 0xC4, 0xCC, 0xD4, 0xDC, 0xE4, 0xEC, 0xF4, 0xFC)


def _code_like(data: bytes, bank: int) -> bool:
    """A bank that reads like code: Z80 code is full of `ret` and `call` (a bank of it has hundreds of each), a bank
    of graphics, maps or text a handful."""
    chunk = data[bank * BANK_SIZE:(bank + 1) * BANK_SIZE]
    return chunk.count(0xC9) + chunk.count(0xCD) >= max(48, len(chunk) // 256)


def _swept(data: bytes, bank: int, base: int, known: set[int], skip=None) -> dict[int, tuple[int, str]]:
    """Function starts named by `call nn` bytes anywhere in `bank` (code nothing reached yet, a table of handlers):
    calls into bank 0, and into the slot the bank's own code runs from (`base`), which is its own bank. Kept only
    when what they point at reads as a function. `skip(flat)`: bytes a correction says aren't code."""
    size, out = len(data), {}
    lo, hi = bank * BANK_SIZE, min((bank + 1) * BANK_SIZE, size)
    for off in range(lo, hi - 2):
        if data[off] not in _CALLS or (skip and skip(off)):
            continue
        target = data[off + 1] | (data[off + 2] << 8)
        if target < 0x4000:
            flat, cpu = target, target
        elif bank and base >= 0x4000 and target >> 14 == base >> 14:
            flat, cpu = bank * BANK_SIZE + (target & (BANK_SIZE - 1)), target
        else:
            continue  # into a slot whose bank depends on what the code mapped: the following finds those
        if flat < size and flat not in known and flat not in out and _clean(data, flat, cpu, _extent(data, flat, cpu, size, known)):
            out[flat] = (cpu, "call")
    return out


def candidates(data: bytes, limit: int = 4000, labels: dict | None = None,
               hints: dict | None = None) -> tuple[dict[int, tuple[int, str]], dict[int, int]]:
    """({flat offset: (CPU address it runs at, how it was found)}, {bank: CPU address its code runs at}).

    Bank 0 is always at 0x0000, so the calls into it, wherever they are, name functions there; with the vectors (and
    a symbol file's labels, `labels`) they seed following the code with the banks it maps (sms_trace): that finds
    the functions it reaches, and where each bank's code runs. Then every other bank is swept for calls within the
    slot it runs from, and the code followed again from all of it. `hints`: the project's corrections (functions).
    """
    hints = hints or {}
    size = len(data)
    seeds = {v: (v, "vector") for v in VECTORS if v < size}
    seeds.update({f: (cpu, "hint") for f, cpu in (hints.get("entries") or {}).items() if f < size})
    if labels:
        seeds.update({f: c for f, c in sms_trace.sym_seeds(labels, data).items() if f not in seeds})
    cov = hints.get("coverage")
    in_data = _data_mask(size, list(hints.get("data") or []) + (cov.runs("read") if cov is not None else []))
    seeds.update({f: c for f, c in _swept(data, 0, 0, set(seeds), in_data).items() if f not in seeds})
    # a first pass learns which slots the game pages; then code entered from anywhere nothing was followed from
    # doesn't assume the power-on bank in those (a call there would land in a data bank)
    probe = sms_trace.Tracer(data, hints=hints).follow(seeds)
    tracer = sms_trace.Tracer(data, paged={s for s, n in probe.slot_writes.items() if n}, hints=hints)
    found = tracer.follow(seeds)
    bases = dict(found.bank_base)
    for bank in range(1, -(-size // BANK_SIZE)):  # banks nothing was followed into: where they most likely run
        if bank not in bases and (base := tracer.default_base(bank)) is not None:
            bases[bank] = base
    handlers = {f: c for f, c in _hint_tables(data, hints, bases).items() if f not in seeds}
    if cov is not None:  # where calls entered while the game ran, at the address its bank runs at
        handlers.update({f: (_cpu_at(f, bases, size), "ran") for f in sorted(cov.entries) if f not in seeds and f < size})
    if handlers:  # (the tables corrections name, read in the banks their code was found running at)
        seeds.update(handlers)
        found = sms_trace.Tracer(data, paged={s for s, n in probe.slot_writes.items() if n}, hints=hints).follow(seeds)
        bases = {**bases, **found.bank_base}
    more = dict(found.starts)
    code_banks = {flat // BANK_SIZE for flat in found.starts}
    for bank in range(1, -(-size // BANK_SIZE)):
        if bank in code_banks or _code_like(data, bank):  # (in a bank of graphics or text, CD bytes aren't calls)
            more.update(_swept(data, bank, bases.get(bank, slot_base(bank, size)), set(more), in_data))
    paged = {s for s, n in probe.slot_writes.items() if n}
    if len(more) > len(found.starts):
        found = sms_trace.Tracer(data, paged=paged, hints=hints).follow({**more, **seeds})
        bases = {**bases, **found.bank_base}
    if cov is not None:  # code that ran but wasn't followed (reached through a pointer): where each run of it starts
        ran = {f: (_cpu_at(f, bases, size), "ran") for f in range(size) if cov.code[f] and (f == 0 or not cov.code[f - 1])
               and f not in found.code and f not in more and f not in seeds}
        if ran:
            more.update(ran)
            found = sms_trace.Tracer(data, paged=paged, hints=hints).follow({**more, **seeds})
            bases = {**bases, **found.bank_base}
    # code nothing points at (called through pointers kept in RAM, which this can't follow): what starts right where
    # a function ends, in a bank that has code, and reads like a function, is the next one. While that finds more.
    seeds = {**more, **seeds}
    for _ in range(8):
        gaps = _after_functions(data, found.starts, hints.get("inline"))
        if not gaps:
            break
        seeds.update(gaps)
        found = sms_trace.Tracer(data, paged=paged, hints=hints).follow(seeds)
        bases = {**bases, **found.bank_base}
    starts = dict(sorted(found.starts.items())[:limit])
    return starts, bases


def _data_mask(size: int, spans: list[tuple[int, int]]):
    """`flat -> bool` for bytes in `spans` ((flat, size) each), or None when there are none."""
    if not spans:
        return None
    mask = bytearray(size)
    for a, n in spans:
        mask[a:a + n] = b"\x01" * max(0, min(n, size - a))
    return lambda flat: 0 <= flat < size and mask[flat] == 1


def _cpu_at(flat: int, bases: dict[int, int], size: int) -> int:
    """The CPU address code at ROM offset `flat` runs at: bank 0 where it is, another bank where it was found running
    (else where it most likely runs)."""
    bank = flat // BANK_SIZE
    return flat if bank == 0 else bases.get(bank, slot_base(bank, size)) + (flat & (BANK_SIZE - 1))


def _hint_tables(data: bytes, hints: dict, bases: dict[int, int]) -> dict[int, tuple[int, str]]:
    """{flat: (cpu, "hint")} for the handlers in the tables corrections name: each word is an address in bank 0, or
    in the slot the table's own bank runs from (its own bank)."""
    out = {}
    for flat, count in hints.get("tables") or []:
        bank = flat // BANK_SIZE
        base = bases.get(bank, slot_base(bank, len(data))) if bank else 0
        for k in range(count):
            at = flat + 2 * k
            if at + 1 >= len(data):
                break
            word = data[at] | data[at + 1] << 8
            if word < 0x4000:
                to = word
            elif bank and base >= 0x4000 and word >> 14 == base >> 14:
                to = bank * BANK_SIZE + (word & (BANK_SIZE - 1))
            else:
                continue  # into a slot whose bank the table doesn't say
            if to < len(data):
                out[to] = (word, "hint")
    return out


def _after_functions(data: bytes, starts: dict[int, tuple[int, str]], inline: dict | None = None) -> dict[int, tuple[int, str]]:
    """{flat: (cpu, "after")} for plausible function starts right where a function ends, where no function starts
    yet and no function's bytes run on. (`inline`: routines that read bytes after their call, as for _walk.)"""
    size, order = len(data), sorted(starts)
    known = set(order)
    spans = []
    for i, start in enumerate(order):
        cpu = starts[start][0]
        hard = order[i + 1] if i + 1 < len(order) else size
        spans.append((start, start + len(_extent(data, start, cpu, hard, known, inline)), cpu))
    out = {}
    for k, (start, end, cpu) in enumerate(spans):
        if end == start or end in known or end % BANK_SIZE == 0 or end >= size:
            continue
        if k + 1 < len(spans) and spans[k + 1][0] < end:
            continue  # (the next function starts inside this one's bytes)
        here = cpu + (end - start)
        if sms_trace.plausible(data, end, here):
            out[end] = (here, "after")
    return out


def _instructions(code: bytes, cpu: int, operands: dict | None = None):
    """(address, text, length) of the instructions in `code`, leaving out `operands` ({address: length})."""
    off = 0
    while off < len(code):
        addr = cpu + off
        if operands and (n := operands.get(addr)):
            off += n
            continue
        text, ln = z80.decode(code[off:off + 4], addr)
        yield addr, text, ln
        off += max(1, ln)


def refs_of(code: bytes, cpu: int, bank: int, size: int, operands: dict | None = None) -> list[tuple[int, int, int]]:
    """(offset in the function, 10, CPU address) for every call and jump out of `code`, as the other lanes record
    them (not in `operands`, {address: length}: bytes after a call that aren't instructions)."""
    out: list[tuple[int, int, int]] = []
    for a, text, _ln in _instructions(code, cpu, operands):
        kind, target = z80.flow(text)
        if target is None or kind not in ("call", "jump", "cjump"):
            continue
        flat = target_flat(target, bank, size, a)
        if flat is not None and 0 <= flat < size and not (cpu <= target < cpu + len(code)):
            out.append((a - cpu, 10, target))
    return out


def functions(data: bytes, log=lambda *_: None, labels: dict | None = None,
              report: dict | None = None, seeds: dict | None = None, hints: dict | None = None) -> list[gb.GbFunction]:
    """Every discovered function, disassembled at the address it runs at. (The record type is shared with the Game
    Boy lane.) `labels` ({(bank, address): name}, from a symbol file) name what they label, and with `seeds` (the
    same, with the file's automatic names too) say where code starts; `report` gets {"bank_base": {bank: CPU
    address}} for the banks whose code was found running somewhere, and {"regions": [(flat, size, kind, why)]} for
    what isn't code. `hints`: the project's corrections, as engine._hints gives them: {"entries": {flat: cpu},
    "data": [(flat, count)], "tables": [(flat, count)], "inline": {flat: count}, "noreturn": {flat}}."""
    hints = hints or {}
    found, bases = candidates(data, labels=seeds if seeds is not None else labels, hints=hints)
    if not found:
        raise RomError("no code found in this ROM: every candidate address pointed outside it")
    size = len(data)
    cov = hints.get("coverage")
    read = cov.runs("read") if cov is not None else []
    spans = sorted(list(hints.get("data") or []) + read)
    in_data = _data_mask(size, spans) or (lambda _flat: False)
    span_starts = [a for a, _n in spans]
    starts = sorted(f for f in found if not in_data(f))
    known = set(starts)
    inline = hints.get("inline") or {}
    regions = [(a, n, "hint", "a correction says these bytes aren't code") for a, n in hints.get("data") or []]
    regions += [(a, n, "read", "read as data while the game ran (coverage)") for a, n in read]
    taken: set[str] = set()
    out: list[gb.GbFunction] = []
    for i, start in enumerate(starts):
        cpu = found[start][0]
        hard = starts[i + 1] if i + 1 < len(starts) else min(start + MAX_FUNCTION, size)
        k = bisect.bisect_right(span_starts, start)  # (a function stops where data starts)
        hard = min(hard, span_starts[k]) if k < len(span_starts) else hard
        steps = list(_walk(data, start, cpu, hard, known, inline))
        code = data[start:steps[-1][0] + max(1, steps[-1][2])] if steps else b""
        operands = {cpu + off - start: n for off, text, n in steps if text == "db"}  # (CPU address: length)
        regions += [(off, n, "inline", "read by the routine called just before") for off, text, n in steps if text == "db"]
        insns = list(_instructions(code, cpu, operands))
        if not insns:
            continue
        label = (labels or {}).get((bank_of(start), cpu))
        name = label if label and label not in taken and re.fullmatch(r"[A-Za-z_]\w*", label) else \
            function_name(start, size, cpu)
        taken.add(name)
        out.append(gb.GbFunction(name=name, vram=cpu, rom=start, bank=bank_of(start), code=code,
                                 n_insns=len(insns),
                                 asm=z80.asm_text(code, cpu, width=4, data=operands),
                                 refs=refs_of(code, cpu, bank_of(start), size, operands)))
    if report is not None:
        report["bank_base"] = bases
        if regions:
            report["regions"] = regions
    moved = sorted(b for b, base in bases.items() if base != slot_base(b, size))
    log(f"found {len(out)} functions in {-(-size // BANK_SIZE)} banks"
        + (f"; code in bank{'s' if len(moved) > 1 else ''} {', '.join(map(str, moved))} runs at the address the "
           "game maps it to, not the power-on one" if moved else ""))
    return out


def split(path: Path, workdir: Path, log=lambda *_: None, report: dict | None = None,
          hints: dict | None = None) -> list[gb.GbFunction]:
    """A ROM file's functions. (`report` gets {"splitter": "built-in"}: there is no external tool for the Z80, and
    the banks' addresses.) A WLA-DX symbol file next to the ROM (`game.sym` for `game.sms`) seeds and names them;
    `hints` are the project's corrections (functions)."""
    if report is not None:
        report["splitter"] = "built-in"
    data = strip_copier_header(Path(path).read_bytes())
    if not is_sms(data, Path(path).name):
        raise RomError("this isn't a Master System or Game Gear ROM")
    info = read_info(data, Path(path).name)
    log(f"{info['platform'].upper()}: {info['title']} ({info['cartridge']}"
        f"{', header found' if info['header'] else ', no header'})")
    labels = seeds = None
    sym = Path(path).with_suffix(".sym")
    if sym.is_file():
        text = sym.read_text(encoding="utf-8", errors="replace")
        labels, seeds = sms_trace.read_sym(text), sms_trace.read_sym(text, seeds=True)
        log(f"{len(labels)} labels from {sym.name}" + (f", and {len(seeds) - len(labels)} places it says code starts"
                                                         if len(seeds) > len(labels) else ""))
    if hints and any(hints.get(k) for k in ("entries", "data", "tables", "inline", "noreturn")):
        log("with the project's corrections")
    if hints and hints.get("coverage") is not None:
        log("with what an emulator saw the game run")
    return functions(data, log=log, labels=labels, report=report, seeds=seeds, hints=hints)
