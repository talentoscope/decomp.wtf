"""Nintendo Entertainment System / Famicom (.nes) ROMs: the iNES and NES 2.0 header, a banking model for the common
mappers, and function discovery on top of the fork's own 6502 decoder (`mos6502.py`).

What it covers: reading, disassembly and function discovery, and with the pinned GBDK-2020 toolchain installed the
matching lane (`gb_build` with `arch="mos6502"`: SDCC's 6502 back end, byte-compared against the ROM). Nearly every
commercial NES game was written in assembly, so the lane's real use is homebrew built with GBDK-2020 or SDCC; for
the rest this is a reader, a navigator and a scorer for attempts, not a promise of a match.

The file. An iNES file is a 16-byte header, an optional 512-byte trainer, the PRG ROM (the 6502's code and data, in
16 KiB units), then the CHR ROM (graphics). Only the PRG holds code. Every offset this module returns is an offset in
the *file* (header included), so it can be found in a hex editor; the banking maths runs on offsets within the PRG.

Banking. Which PRG bank the CPU sees at an address is set by writes to mapper registers, and that is not in the ROM,
so the model is the common arrangement of each mapper, the way the Game Boy lane shows its banks:
  * NROM (0), CNROM (3): the PRG at 0x8000 (a 16 KiB one is shown at 0xC000, where its vectors are, and mirrored at 0x8000).
  * MMC1 (1), UxROM (2), UNROM 512 (30), 94 and the many look-alikes: 16 KiB banks, the last one fixed at 0xC000, the
    others shown at 0x8000. (An unknown mapper is read this way too.)
  * Mapper 180: the first bank fixed at 0x8000, the others at 0xC000.
  * MMC1 with more than 256 KiB of PRG (SUROM, SOROM, SXROM): each 256 KiB half has its last bank fixed at 0xC000.
  * Mapper 180 and 92: the first bank fixed at 0x8000, the others at 0xC000; 97: the last fixed at 0x8000.
  * MMC3 (4, 118, 119): 8 KiB banks, the last two fixed at 0xC000 and 0xE000, the rest shown at 0x8000.
  * 8 KiB banks with the last fixed at 0xE000 and three windows switched (Namco 163, FME-7, VRC7, MMC5, Jaleco
    SS88006, Irem H3001, Taito X1-005); with the last 16 KiB fixed (VRC2, VRC4, Irem G-101, Taito TC0190/TC0690);
    with the last 24 KiB fixed (MMC2). Switched banks are shown at 0x8000.
  * VRC6 (24, 26): a 16 KiB bank at 0x8000 (8 KiB units: even ones at 0x8000, odd at 0xA000), the last 8 KiB fixed.
  * Mapper 31: 4 KiB banks, each at its slot of eight (bank n at 0x8000 + 0x1000 * (n % 8)), the last at 0xF000.
  * AxROM (7), GxROM (66), BNROM (34), 11, 79: 32 KiB banks at 0x8000.
A switched window reached from code in another bank is left unresolved: which bank is there is the game's choice.
The reset, NMI and IRQ vectors are the last six bytes of the PRG. A function's name spells its bank and CPU address
(`func_<bank>_<address>`), because two banks can hold code at the same address.

Function discovery is approximate by nature: the 6502 has no code/data separation, an indirect `jmp ($xxxx)` has no
visible target, and a game can dispatch through a table of addresses it pushes and returns to. The vectors are
followed through every `jsr` and `jmp`; `jsr` targets in code the descent never reached are kept when they read as a
function; and runs of three or more addresses (a handler table, or the address-minus-one form `rts` dispatch uses)
that each point at a function are taken as one.
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import gb, mos6502
from .n64 import RomError

MAGIC = b"NES\x1a"
HEADER_SIZE = 16
TRAINER_SIZE = 512
PRG_UNIT = 0x4000
CHR_UNIT = 0x2000
MAX_FUNCTION = 0x1000  # a function longer than this is more likely a run of code the sweep never ended
MIN_TABLE = 3          # addresses in a row that make a handler table

#: mapper number -> (name, banking model). See the module docstring; anything not listed is read like UxROM.
MAPPERS: dict[int, tuple[str, str]] = {
    0: ("NROM", "nrom"), 1: ("MMC1", "last"), 2: ("UxROM", "last"), 3: ("CNROM", "nrom"),
    4: ("MMC3", "mmc3"), 5: ("MMC5", "w8a"), 7: ("AxROM", "all"), 9: ("MMC2", "w8c"), 10: ("MMC4", "last"),
    11: ("Color Dreams", "all"), 18: ("Jaleco SS88006", "w8a"), 19: ("Namco 163", "w8a"), 21: ("VRC4", "w8b"),
    22: ("VRC2", "w8b"), 23: ("VRC2/4", "w8b"), 24: ("VRC6", "vrc6"), 25: ("VRC4", "w8b"), 26: ("VRC6", "vrc6"),
    30: ("UNROM 512", "last"), 31: ("NSF-style", "k4"), 32: ("Irem G-101", "w8b"), 33: ("Taito TC0190", "w8b"),
    34: ("BNROM", "all"), 48: ("Taito TC0690", "w8b"), 65: ("Irem H3001", "w8a"), 66: ("GxROM", "all"),
    69: ("FME-7", "w8a"), 71: ("Camerica", "last"), 79: ("NINA-03/06", "all"), 80: ("Taito X1-005", "w8a"),
    85: ("VRC7", "w8a"), 92: ("Jaleco JF-19", "first"), 94: ("UN1ROM", "last"), 97: ("Irem TAM-S1", "lastlow"),
    118: ("TxSROM", "mmc3"), 119: ("TQROM", "mmc3"), 180: ("UNROM (reversed)", "first"), 206: ("DxROM", "mmc3"),
    207: ("Taito X1-005", "w8a"),
}
_UNIT = {"nrom": PRG_UNIT, "last": PRG_UNIT, "first": PRG_UNIT, "lastlow": PRG_UNIT, "surom": PRG_UNIT, "mmc3": 0x2000,
         "w8a": 0x2000, "w8b": 0x2000, "w8c": 0x2000, "vrc6": 0x2000, "k4": 0x1000, "all": 0x8000}
# 8 KiB-window mappers: what $8000, $A000, $C000 and $E000 show: None (a switched bank) or a fixed one, counted from
# the end (-1: the last)
_WINDOWS = {"w8a": (None, None, None, -1), "w8b": (None, None, -2, -1), "w8c": (None, -3, -2, -1),
            "vrc6": (None, None, None, -1)}


# ---------------------------------------------------------------- the header

def is_nes(data: bytes) -> bool:
    return len(data) >= HEADER_SIZE + 0x2000 and data[:4] == MAGIC


def parse_header(data: bytes) -> dict:
    """The iNES / NES 2.0 header as numbers. Raises RomError for a file this module cannot read."""
    if not is_nes(data):
        raise RomError("this isn't an NES ROM (no iNES header)")
    flags6, flags7 = data[6], data[7]
    nes2 = (flags7 & 0x0C) == 0x08
    # Old dumps have a tool's name in bytes 7-15 ("DiskDude!"): byte 7 is then not flags, and the mapper's high nibble is 0.
    dirty = not nes2 and any(data[12:16])
    mapper = (flags6 >> 4) | (0 if dirty else flags7 & 0xF0)
    prg_units, chr_units = data[4], data[5]
    submapper = 0
    if nes2:
        mapper |= (data[8] & 0x0F) << 8
        submapper = data[8] >> 4
        if data[9] & 0x0F == 0x0F or data[9] >> 4 == 0x0F:
            raise RomError("this NES 2.0 ROM states its size as an exponent, which isn't supported")
        prg_units |= (data[9] & 0x0F) << 8
        chr_units |= (data[9] >> 4) << 8
    trainer = bool(flags6 & 4)
    prg_off = HEADER_SIZE + (TRAINER_SIZE if trainer else 0)
    prg_size = prg_units * PRG_UNIT
    if prg_size == 0:
        raise RomError("this NES ROM has no program (the header says 0 PRG banks)")
    available = len(data) - prg_off
    truncated = available < prg_size
    if truncated:  # a cut-off dump: read what is there, whole 8 KiB at a time
        prg_size = available // 0x2000 * 0x2000
        if prg_size == 0:
            raise RomError("this NES ROM is cut off before its program ends")
    return {
        "mapper": mapper, "submapper": submapper, "nes2": nes2, "trainer": trainer,
        "prg_off": prg_off, "prg_size": prg_size, "prg_units": prg_units, "truncated": truncated,
        "chr_size": chr_units * CHR_UNIT, "battery": bool(flags6 & 2),
        "mirroring": "four-screen" if flags6 & 8 else "vertical" if flags6 & 1 else "horizontal",
        "region": ("PAL" if data[12] & 3 == 1 else "NTSC" if data[12] & 3 == 0 else "multi-region" if data[12] & 3 == 2
                   else "Dendy") if nes2 else ("PAL" if not dirty and data[9] & 1 else "NTSC"),
    }


def read_info(data: bytes, name: str = "") -> dict:
    """The cartridge header, in the shape the rest of the fork expects from a ROM reader."""
    h = parse_header(data)
    layout = layout_of(h)
    mapper_name = MAPPERS.get(h["mapper"], (f"mapper {h['mapper']}", "last"))[0]
    known = h["mapper"] in MAPPERS
    stem = Path(name).stem if name else ""
    prg_kb = h["prg_size"] // 1024
    chr_text = f"{h['chr_size'] // 1024} KiB CHR" if h["chr_size"] else "CHR RAM"
    return {
        "title": stem or "NES ROM",
        "game_code": "",
        "region": h["region"],
        "version": 0,
        "size": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
        "entrypoint": 0,
        "platform": "nes",
        "byte_order": "little",
        "compiler_guess": "a 6502 assembler, cc65 or SDCC (unknown)",
        "gp": 0,
        "header": True,
        "cartridge": f"{mapper_name}{'' if known else ' (read as UxROM)'} (mapper {h['mapper']}), {prg_kb} KiB PRG, "
                     f"{chr_text}, {h['mirroring']}{', battery' if h['battery'] else ''}",
        "rom_banks": layout.banks,
        "bank_size": layout.unit,
        "mapper": h["mapper"],
        "prg_offset": h["prg_off"],
        "prg_size": h["prg_size"],
        "chr_size": h["chr_size"],
        "nes2": h["nes2"],
        "truncated": h["truncated"],
    }


# ---------------------------------------------------------------- banking

@dataclass(frozen=True)
class Layout:
    """How a mapper shows the PRG to the CPU (see the module docstring)."""

    kind: str   # nrom | last | first | mmc3 | all
    unit: int   # the size of one bank
    size: int   # the PRG's size in bytes

    @property
    def banks(self) -> int:
        return max(1, -(-self.size // self.unit))

    def base(self, bank: int) -> int:
        """The CPU address where `bank` is shown."""
        n = self.banks
        if self.kind in _WINDOWS:
            fixed = {n + k: 0x8000 + 0x2000 * w for w, k in enumerate(_WINDOWS[self.kind]) if k is not None and n + k >= 0}
            if bank in fixed:
                return fixed[bank]
            return 0xA000 if self.kind == "vrc6" and bank % 2 else 0x8000  # (VRC6: a 16 KiB bank, in 8 KiB halves)
        if self.kind == "k4":
            return 0xF000 if bank == n - 1 else 0x8000 + 0x1000 * (bank % 8)
        if self.kind == "surom":
            return 0xC000 if bank % 16 == 15 or bank == n - 1 else 0x8000
        if self.kind == "lastlow":
            return 0x8000 if bank == n - 1 else 0xC000
        if self.kind == "nrom":
            # a 16 KiB program is shown at 0xC000: its vectors are at 0xFFFA, so that is where its code is assembled
            return 0xC000 if self.size <= PRG_UNIT else 0x8000 + PRG_UNIT * bank
        if self.kind == "last":
            return 0xC000 if bank == self.banks - 1 else 0x8000
        if self.kind == "first":
            return 0x8000 if bank == 0 else 0xC000
        if self.kind == "mmc3":
            return 0xE000 if bank == self.banks - 1 else 0xC000 if bank == self.banks - 2 else 0x8000
        return 0x8000

    def bank_of(self, p: int) -> int:
        return p // self.unit



    def cpu(self, p: int) -> int:
        """The CPU address of the byte at PRG offset `p`."""
        return self.base(p // self.unit) + p % self.unit

    def target(self, cpu: int, from_bank: int) -> int | None:
        """The PRG offset a jump or call to `cpu` reaches from code in `from_bank`, or None for RAM, registers and
        addresses nothing is mapped at."""
        if cpu < 0x8000:
            return None
        n, unit, p = self.banks, self.unit, None
        if self.kind in _WINDOWS or self.kind == "k4":
            window = cpu & ~(unit - 1)
            fixed = _fixed_windows(self.kind, n)
            if window in fixed:
                p = fixed[window] * unit + (cpu & (unit - 1))
            elif self.base(from_bank) == window:
                p = from_bank * unit + (cpu & (unit - 1))  # (its own window: its own bank)
            return p if p is not None and 0 <= p < self.size else None
        if self.kind == "surom":  # each 256 KiB half's last bank is at 0xC000
            half = from_bank // 16 * 16
            if cpu >= 0xC000:
                p = min(half + 15, n - 1) * unit + cpu - 0xC000
            elif self.base(from_bank) == 0x8000:
                p = from_bank * unit + cpu - 0x8000
            return p if p is not None and 0 <= p < self.size else None
        if self.kind == "lastlow":
            if cpu < 0xC000:
                p = (n - 1) * unit + cpu - 0x8000
            elif self.base(from_bank) == 0xC000:
                p = from_bank * unit + cpu - 0xC000
            return p if p is not None and 0 <= p < self.size else None
        if self.kind == "nrom":
            p = (cpu - 0x8000) % PRG_UNIT if self.size <= PRG_UNIT else cpu - 0x8000
        elif self.kind == "last":
            if cpu >= 0xC000:
                p = (n - 1) * unit + cpu - 0xC000
            elif n > 1:
                bank = from_bank if self.base(from_bank) == 0x8000 else 0
                p = bank * unit + cpu - 0x8000
        elif self.kind == "first":
            if cpu < 0xC000:
                p = cpu - 0x8000
            elif n > 1:
                bank = from_bank if self.base(from_bank) == 0xC000 else 1
                p = bank * unit + cpu - 0xC000
        elif self.kind == "mmc3":
            if cpu >= 0xE000:
                p = (n - 1) * unit + cpu - 0xE000
            elif cpu >= 0xC000 and n >= 2:
                p = (n - 2) * unit + cpu - 0xC000
            elif cpu >= 0xA000 and n >= 4:
                p = unit + cpu - 0xA000  # the second window: which bank is there is the game's choice; bank 1 is a guess
            elif cpu < 0xA000 and n >= 3:
                bank = from_bank if self.base(from_bank) == 0x8000 else 0
                p = bank * unit + cpu - 0x8000
        else:  # "all": the bank is the whole CPU window
            p = from_bank * unit + cpu - 0x8000
        return p if p is not None and 0 <= p < self.size else None


@functools.lru_cache(maxsize=64)
def _fixed_windows(kind: str, n: int) -> dict[int, int]:
    """{CPU address of a window: the bank always shown there} for a mapper with fixed windows."""
    if kind == "k4":
        return {0xF000: n - 1}
    return {0x8000 + 0x2000 * w: n + k for w, k in enumerate(_WINDOWS[kind]) if k is not None and n + k >= 0}


def layout_of(header: dict) -> Layout:
    kind = MAPPERS.get(header["mapper"], ("", "last"))[1]
    if kind == "nrom" and header["prg_size"] > 2 * PRG_UNIT:
        kind = "last"  # a CNROM header with more than 32 KiB of PRG isn't one
    if header["mapper"] == 1 and header["prg_size"] > 16 * PRG_UNIT:
        kind = "surom"  # MMC1 over 256 KiB (SUROM, SOROM, SXROM): two halves, each with its last bank fixed
    return Layout(kind, _UNIT[kind], header["prg_size"])


def segments(info: dict) -> list[dict]:
    """The project's segments: one per PRG bank, at the CPU address the bank is shown at."""
    layout = Layout(layout_of({"mapper": info["mapper"], "prg_size": info["prg_size"]}).kind, info["bank_size"], info["prg_size"])
    off = info["prg_offset"]
    return [{"name": f"bank{b:03d}", "rom_start": off + b * layout.unit,
             "rom_end": off + min((b + 1) * layout.unit, layout.size), "vram": layout.base(b)}
            for b in range(layout.banks)]


def function_name(p: int, layout: Layout) -> str:
    return f"func_{layout.bank_of(p):03d}_{layout.cpu(p):04x}"


def vectors(prg: bytes, layout: Layout) -> dict[str, int]:
    """{"nmi" | "reset" | "irq": PRG offset}: the three vectors in the last six bytes of the PRG, for those that point
    into the ROM."""
    out: dict[str, int] = {}
    last = layout.banks - 1
    for i, name in enumerate(("nmi", "reset", "irq")):
        word = prg[len(prg) - 6 + 2 * i] | (prg[len(prg) - 5 + 2 * i] << 8)
        p = layout.target(word, last)
        if p is not None:
            out[name] = p
    return out


# ---------------------------------------------------------------- function discovery

_COMPLEMENT = {"bcc": "bcs", "bcs": "bcc", "beq": "bne", "bne": "beq", "bmi": "bpl", "bpl": "bmi", "bvc": "bvs",
               "bvs": "bvc"}


def _always(prev: str, text: str) -> bool:
    """Whether a branch is always taken, after the instruction before it: its complement (`bcs x / bcc y`: one of
    them goes), or one that sets the flag it reads to a constant (`sec / bcs`, `lda #0 / beq`). A table of data can
    follow it, as it does a `jmp`."""
    op = text.split(" ", 1)[0]
    if op not in _COMPLEMENT:
        return False
    if prev.split(" ", 1)[0] == _COMPLEMENT[op]:
        return True
    if (prev, op) in (("sec", "bcs"), ("clc", "bcc"), ("clv", "bvc")):
        return True
    if (m := re.match(r"^ld[axy] #\$([0-9a-f]{2})$", prev)):
        v = int(m.group(1), 16)
        return {"beq": v == 0, "bne": v != 0, "bmi": v >= 0x80, "bpl": v < 0x80}.get(op, False)
    return False


def _walk(prg: bytes, layout: Layout, start: int, hard: int, starts: set[int], engines: dict | None = None):
    """Walk the function that begins at PRG offset `start`: yield (offset, text, length) per instruction.

    It ends at a return or an unconditional jump once every forward branch seen so far has been passed, so a branch
    over a `jmp`, or a loop's back-edge, does not end it. A `jmp` is part of the function only when something before it
    already branches past it; otherwise nothing after it can be reached from here and it leaves the function (a tail
    call). A branch that's always taken (`_always`) ends it as a `jmp` does, and so does a call to a routine that
    takes its return address to read what follows the call (`engines`, PRG offset -> "engine" | "inline"): the bytes
    after the call are its, so a branch before that goes past them resumes the walk where it lands, and those bytes
    come as one (offset, "data", length). It never runs past `hard`, `MAX_FUNCTION`, or the end of its bank."""
    bank = layout.bank_of(start)
    limit = start
    off = start
    end = min(hard, start + MAX_FUNCTION, (bank + 1) * layout.unit, len(prg))
    ahead: set[int] = set()  # where forward branches seen so far land
    prev = ""
    while off < end:
        text, ln = mos6502.decode(prg[off:off + 3], layout.cpu(off))
        yield off, text, ln
        nxt = off + max(1, ln)
        kind, target = mos6502.flow(text)
        if kind == "cjump" and _always(prev, text):
            kind = "jump"
        prev = text
        if kind in ("ret", "jump") and nxt > limit:
            return
        if target is not None and kind in ("jump", "cjump"):
            flat = layout.target(target, bank)
            if flat is not None and flat > off and flat not in starts and flat < end:
                limit = max(limit, flat)
                ahead.add(flat)
        if kind == "call" and engines and layout.target(target, bank) in engines:
            if nxt > limit:
                return  # (it doesn't come back here: what follows the call is the routine's)
            resume = min(x for x in ahead if x > off)
            if resume > nxt:
                yield nxt, "data", resume - nxt
            off = resume
            continue
        off = nxt


def _extent(prg: bytes, layout: Layout, start: int, hard: int, starts: set[int], engines: dict | None = None) -> bytes:
    """A function's bytes: from its start up to and including the instruction that ends it, never past `hard`.

    Exact extents matter for matching: a candidate compiled from C produces the function and nothing else."""
    end = start
    for off, _text, ln in _walk(prg, layout, start, hard, starts, engines):
        end = off + max(1, ln)
    return prg[start:end]


def reads_return(prg: bytes, layout: Layout, start: int) -> str | None:
    """"engine" for a routine that takes its return address to read a table of handlers that follows its call (SMB's
    jump engine: `asl / tay / pla / sta $04 / pla / sta $05 / iny / lda ($04),y / ... jmp ($0006)`), "inline" for one
    that reads other bytes after its call through it, None for any other (a `pla` that restores a register isn't
    this: the two bytes go to a zero-page pointer, low then high, that it then reads through)."""
    insns = list(mos6502.disassemble(prg[start:start + 64], layout.cpu(start), count=24))
    stored, pulled, first = [], False, True
    for _a, text, _l in insns:
        kind, _t = mos6502.flow(text)
        if text == "pla" and first:
            pulled = True
            continue
        if pulled and (m := re.match(r"^sta \$([0-9a-f]{2})$", text)):
            stored.append(int(m.group(1), 16))
            pulled = False
            continue
        pulled = False
        if text.startswith(("pha", "php", "jsr")) or kind in ("cjump", "jump", "ret"):
            first = False
    if len(stored) < 2 or stored[1] != stored[0] + 1:
        return None
    if not any(t == f"lda (${stored[0]:02x}),y" or t.startswith(f"jmp (${stored[0]:04x})") for _a, t, _l in insns):
        return None
    return "engine" if any(t.startswith("jmp (") for _a, t, _l in insns) else "inline"


def _handlers(prg: bytes, layout: Layout, at: int, known: set[int], limit: int = 64) -> list[int]:
    """The handlers in the table at PRG offset `at` (after a call to a jump engine): each word up to the first that
    isn't an instruction in the ROM, or where known code starts (the table ended there)."""
    out, bank = [], layout.bank_of(at)
    for k in range(limit):
        p = at + 2 * k
        if p + 1 >= len(prg) or layout.bank_of(p + 1) != bank or (k and p in known):
            break
        word = prg[p] | prg[p + 1] << 8
        flat = layout.target(word, bank) if word >= 0x8000 else None
        if flat is None or mos6502.OPCODES.get(prg[flat]) is None:
            break
        out.append(flat)
    return out


def _clean(layout: Layout, start: int, code: bytes) -> int:
    """How many instructions `code` is if it reads as a function, else 0: no undefined opcode, no `brk` (which is what
    zero padding and empty data decode to, and which a game almost never executes on purpose), and it ends on a
    `rts`, `rti` or `jmp`."""
    if not code:
        return 0
    insns = list(mos6502.disassemble(code, layout.cpu(start)))
    if not insns or any(t.startswith(("db ", "brk")) for _a, t, _l in insns):
        return 0
    return len(insns) if mos6502.is_return(insns[-1][1]) else 0


def candidates(prg: bytes, layout: Layout, limit: int = 6000, coverage=None,
               engines: dict | None = None, tables: dict | None = None) -> dict[int, str]:
    """{PRG offset: how it was found} for every plausible function start: the vectors, then everything the code
    found calls or tail-jumps to (recursive descent), then `jsr` targets from code the descent never reached and
    handler tables, each kept only when what it points at reads as a function. `coverage` (rom/coverage.py, by PRG
    offset): where calls entered while the game ran start functions, and so does code that ran but wasn't reached;
    nothing starts in what it only read as data. A routine that takes its return address to read what follows its
    call (`reads_return`) ends its callers' straight-line code there; a jump engine's table after each call to it is
    read, its handlers functions. Found that way, the descent runs again knowing them (`engines` gets them, PRG offset
    -> "engine" | "inline"; `tables` the tables, PRG offset -> bytes)."""
    engines = {} if engines is None else engines
    tables = {} if tables is None else tables
    for _ in range(4):
        tables.clear()
        found = _discover(prg, layout, limit, coverage, engines, tables)
        more = {p: k for p in found if p not in engines and (k := reads_return(prg, layout, p))}
        if not more:
            break
        engines.update(more)
    return found


def _discover(prg: bytes, layout: Layout, limit: int, coverage, engines: dict, tables: dict) -> dict[int, str]:
    """One descent (see candidates). `tables` gets the jump engines' tables it read: PRG offset -> bytes."""
    found: dict[int, str] = {}
    for name, p in vectors(prg, layout).items():
        found.setdefault(p, name)
    data = bytearray(len(prg))
    for a, n in coverage.runs("read") if coverage is not None else ():
        data[a:a + n] = b"\x01" * n
    for p in sorted(coverage.entries) if coverage is not None else ():
        if p < len(prg):
            found.setdefault(p, "ran")

    def follow(start: int) -> None:
        todo = [start]
        while todo and len(found) < limit:
            s = todo.pop()
            bank = layout.bank_of(s)
            steps = list(_walk(prg, layout, s, len(prg), set(found), engines))
            end = steps[-1][0] + max(1, steps[-1][2]) if steps else s
            for off, text, ln in steps:
                if text == "data":
                    continue
                kind, target = mos6502.flow(text)
                if target is None or kind not in ("call", "jump", "cjump"):
                    continue
                flat = layout.target(target, bank)
                if flat is not None and kind == "call" and engines.get(flat) == "engine" and off + ln not in tables:
                    handlers = _handlers(prg, layout, off + ln, set(found))
                    tables[off + ln] = 2 * len(handlers)
                    data[off + ln:off + ln + 2 * len(handlers)] = b"\x01" * (2 * len(handlers))
                    for h in handlers:
                        if h not in found:
                            found[h] = "table"
                            todo.append(h)
                if flat is None or flat in found:
                    continue
                if kind == "call" or not (s <= flat < end):
                    found[flat] = "call" if kind == "call" else "jump"
                    todo.append(flat)

    for p in list(found):
        follow(p)
    if coverage is not None:  # code that ran but the descent didn't reach (through a pointer): each run's start
        reached, known = bytearray(len(prg)), set(found)
        for p in known:
            n = len(_extent(prg, layout, p, len(prg), known, engines))
            reached[p:p + n] = b"\x01" * n
        for p in range(len(prg)):
            if coverage.code[p] and (p == 0 or not coverage.code[p - 1]) and not reached[p] and p not in found:
                found[p] = "ran"
                follow(p)

    # `jsr` anywhere in the ROM: a call from code the descent never reached still names a function. One call to a short
    # run of bytes is as likely data that happens to contain 0x20, so it needs a second caller or a few instructions.
    sites: dict[int, int] = {}
    for bank in range(layout.banks):
        lo, hi = bank * layout.unit, min((bank + 1) * layout.unit, len(prg))
        for off in range(lo, hi - 2):
            if prg[off] == 0x20 and not data[off]:
                flat = layout.target(prg[off + 1] | (prg[off + 2] << 8), bank)
                if flat is not None and flat not in found:
                    sites[flat] = sites.get(flat, 0) + 1
    for flat in sorted(sites):
        if flat in found or len(found) >= limit:
            continue
        n = _clean(layout, flat, _extent(prg, layout, flat, len(prg), set(found), engines))
        if n and (sites[flat] >= 2 or n >= 3):
            found[flat] = "call"
            follow(flat)

    # handler tables: three or more addresses in a row, each at a function (or, for `rts` dispatch, one past it)
    for bank in range(layout.banks):
        lo, hi = bank * layout.unit, min((bank + 1) * layout.unit, len(prg))
        for run in _address_runs(prg, layout, bank, lo, hi):
            _take_table(prg, layout, run, found, follow, limit)
    return dict(sorted((p, how) for p, how in found.items() if not data[p]))


def _address_runs(prg: bytes, layout: Layout, bank: int, lo: int, hi: int):
    """Runs of at least MIN_TABLE 16-bit addresses stored one after another in [lo, hi), each pointing into the ROM at
    a byte that starts a valid instruction. Yields lists of the PRG offsets they point at. Both the address itself and
    the address plus one (what `rts` dispatch stores) are tried, at both byte alignments."""
    for plus in (0, 1):
        for parity in (0, 1):
            run: list[int] = []
            for off in range(lo + parity, hi - 1, 2):
                raw = prg[off] | (prg[off + 1] << 8)
                flat = layout.target(raw + plus, bank) if 0x8000 <= raw + plus <= 0xFFFF else None
                if flat is not None and mos6502.OPCODES.get(prg[flat]) is not None:
                    run.append(flat)
                    continue
                if len(run) >= MIN_TABLE:
                    yield run
                run = []
            if len(run) >= MIN_TABLE:
                yield run


def _take_table(prg, layout, run, found, follow, limit) -> None:
    """Keep a run of addresses as functions when most of them read as one."""
    good = [flat for flat in run if _clean(layout, flat, _extent(prg, layout, flat, len(prg), set(found)))]
    if len(good) < len(run) * 0.75:
        return
    for flat in good:
        if flat not in found and len(found) < limit:
            found[flat] = "table"
            follow(flat)


def _inside(prg: bytes, layout: Layout, start: int, at: int, engines: dict) -> bool:
    """Whether PRG offset `at` is the operand of a `bit` in the function at `start`: code skipped into (`bit $04a0`,
    entered at its operand, is `ldy #$04` there), so the function goes on past it."""
    for off, text, ln in _walk(prg, layout, start, at + 3, set(), engines):
        if off < at < off + ln:
            return text.startswith("bit ") and at == off + 1
        if off >= at:
            return False
    return False


def _instructions(code: bytes, cpu: int, operands: dict | None = None):
    """(address, text, length) of the instructions in `code`, leaving out `operands` ({address: length})."""
    off = 0
    while off < len(code):
        addr = cpu + off
        if operands and (n := operands.get(addr)):
            off += n
            continue
        text, ln = mos6502.decode(code[off:off + 3], addr)
        yield addr, text, ln
        off += max(1, ln)


def refs_of(code: bytes, cpu: int, bank: int, layout: Layout, operands: dict | None = None) -> list[tuple[int, int, int]]:
    """(offset in the function, 10, CPU address) for every call and jump out of `code`, as the other lanes record
    them (not in `operands`: bytes after a call that aren't instructions)."""
    out: list[tuple[int, int, int]] = []
    for a, text, _ln in _instructions(code, cpu, operands):
        kind, target = mos6502.flow(text)
        if target is None or kind not in ("call", "jump", "cjump"):
            continue
        flat = layout.target(target, bank)
        if flat is not None and not (cpu <= target < cpu + len(code)):
            out.append((a - cpu, 10, target))
    return out


def functions(data: bytes, log=lambda *_: None, hints: dict | None = None,
              report: dict | None = None) -> list[gb.GbFunction]:
    """Every discovered function, disassembled. (The record type is shared with the Game Boy lane.) Offsets in the
    result are file offsets. `hints`: {"coverage": what an emulator saw the game run and read}; `report` gets
    {"regions": [(file offset, size, "read", why)]} for what it only read as data."""
    h = parse_header(data)
    layout = layout_of(h)
    prg = data[h["prg_off"]:h["prg_off"] + h["prg_size"]]
    cov = (hints or {}).get("coverage")
    engines: dict[int, str] = {}
    tables: dict[int, int] = {}
    found = candidates(prg, layout, coverage=cov, engines=engines, tables=tables)
    if not found:
        raise RomError("no code found in this NES ROM: the reset vector doesn't point into it")
    starts = sorted(found)
    known = set(starts)
    read = cov.runs("read") if cov is not None else []
    read_starts = [a for a, _n in read]
    regions = [(h["prg_off"] + a, n, "read", "read as data while the game ran (coverage)") for a, n in read]
    regions += [(h["prg_off"] + a, n, "table", "a jump engine's table of handlers, after a call to it")
                for a, n in sorted(tables.items()) if n]
    out: list[gb.GbFunction] = []
    for i, start in enumerate(starts):
        k = i + 1  # (a start inside one of its instructions, a `bit` skipped into, doesn't end it)
        while k < len(starts) and _inside(prg, layout, start, starts[k], engines):
            k += 1
        hard = starts[k] if k < len(starts) else len(prg)
        j = bisect.bisect_right(read_starts, start)  # (a function stops where what was only read starts)
        hard = min(hard, read_starts[j]) if j < len(read_starts) else hard
        steps = list(_walk(prg, layout, start, hard, known, engines))
        if not steps:
            continue
        code = prg[start:steps[-1][0] + max(1, steps[-1][2])]
        cpu, bank = layout.cpu(start), layout.bank_of(start)
        operands = {layout.cpu(off): n for off, text, n in steps if text == "data"}  # (CPU address: length)
        regions += [(h["prg_off"] + off, n, "table", "what follows a call to a routine that reads it (a jump engine's "
                     "table, or its operands)") for off, text, n in steps if text == "data"]
        insns = list(_instructions(code, cpu, operands))
        if not insns:
            continue
        out.append(gb.GbFunction(name=function_name(start, layout), vram=cpu, rom=h["prg_off"] + start, bank=bank,
                                 code=code, n_insns=len(insns), asm=mos6502.asm_text(code, cpu, width=4, data=operands),
                                 refs=refs_of(code, cpu, bank, layout, operands)))
    if report is not None and regions:
        report["regions"] = regions
    log(f"found {len(out)} functions in {layout.banks} bank{'s' if layout.banks != 1 else ''} "
        f"({', '.join(f'{n} {layout.cpu(p):04x}' for n, p in vectors(prg, layout).items())})")
    return out


def split(path: Path, workdir: Path, log=lambda *_: None, report: dict | None = None,
          hints: dict | None = None) -> list[gb.GbFunction]:
    """A ROM file's functions. (`report` gets {"splitter": "built-in"}: there is no external tool for the 6502.)
    `hints`: {"coverage": ...}, from a code/data log of the game being played (functions)."""
    if report is not None:
        report["splitter"] = "built-in"
    data = Path(path).read_bytes()
    if not is_nes(data):
        raise RomError("this isn't an NES ROM")
    info = read_info(data, Path(path).name)
    log(f"NES: {info['cartridge']}")
    if info["mapper"] not in MAPPERS:
        log(f"mapper {info['mapper']} is not one this app knows: its banks are shown the way UxROM's are, which may put "
            "some at the wrong CPU address")
    if info["truncated"]:
        log("this ROM is shorter than its header says: reading what is there")
    if (hints or {}).get("coverage") is not None:
        log("with what an emulator saw the game run")
    return functions(data, log=log, hints=hints, report=report)
