"""Game Boy (.gb) and Game Boy Color (.gbc) ROMs: detection, cartridge header, banking and function
discovery, on top of the fork's own SM83 disassembler (see `sm83.py` for why one is needed).

The split is mgbdis's (rom/gb_mgbdis) with the code followed first (rom/gb_trace); this module's own sweep is the
fallback when those tools aren't installed. Matching is SDCC (GBDK-2020) through rom/gb_build, in absolute-symbol
mode: a function's calls and data are pinned to the addresses the ROM has them at.

Addressing note: a function is identified by its **flat ROM offset** (bank 0 is 0x0000-0x3FFF, bank 1 is
0x4000-0x7FFF, and so on) and runs at its CPU address (`cpu_address`): two banks can hold the same CPU address.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import sm83
from .n64 import RomError

# The Nintendo logo the boot ROM compares against: the historical way a Game Boy cartridge is
# recognised, and still the most reliable one for a headered ROM with no other marker.
LOGO = bytes.fromhex("CEED6666CC0D000B03730083000C000D0008111F8889000EDCCC6EE6DD"
                     "DDD999BBBB67636E0EECCCDDDC999FBBB9333E")
HEADER_LEN = 0x150
BANK_SIZE = 0x4000  # 16 KiB


def bank_of(flat: int) -> int:
    """Which 16 KiB bank a flat ROM offset lives in."""
    return flat // BANK_SIZE


def cpu_address(flat: int) -> int:
    """The address the CPU sees for code at this flat offset.

    Bank 0 is wired to 0x0000-0x3FFF; every other bank is switched into the 0x4000-0x7FFF window by the
    MBC. That window is what the ROM's own `call`/`jp` instructions contain, so it is the address a
    candidate's symbols must use (see gb_build's absolute mode)."""
    bank, off = bank_of(flat), flat % BANK_SIZE
    return off if bank == 0 else 0x4000 + off


def flat_offset(cpu: int, bank: int) -> int:
    """The inverse of `cpu_address`: a CPU address in `bank` back to a flat ROM offset."""
    return cpu if cpu < 0x4000 else bank * BANK_SIZE + (cpu - 0x4000)


def function_name(flat: int) -> str:
    """Auto name in bank_cpu form, the convention Game Boy disassembly uses (`Func_001_4234`): with
    banking, two banks can hold the same CPU address, so the address alone is not an identity."""
    return f"func_{bank_of(flat):03d}_{cpu_address(flat):04x}"

ROM_SIZES = {0x00: 0x8000, 0x01: 0x10000, 0x02: 0x20000, 0x03: 0x40000, 0x04: 0x80000,
             0x05: 0x100000, 0x06: 0x200000, 0x07: 0x400000, 0x08: 0x800000,
             0x52: 0x120000, 0x53: 0x140000, 0x54: 0x180000}
RAM_SIZES = {0x00: 0, 0x01: 0x800, 0x02: 0x2000, 0x03: 0x8000, 0x04: 0x20000, 0x05: 0x10000}
MBC = {
    0x00: "ROM only", 0x01: "MBC1", 0x02: "MBC1+RAM", 0x03: "MBC1+RAM+battery",
    0x05: "MBC2", 0x06: "MBC2+battery", 0x08: "ROM+RAM", 0x09: "ROM+RAM+battery",
    0x0B: "MMM01", 0x0C: "MMM01+RAM", 0x0D: "MMM01+RAM+battery",
    0x0F: "MBC3+timer+battery", 0x10: "MBC3+timer+RAM+battery", 0x11: "MBC3",
    0x12: "MBC3+RAM", 0x13: "MBC3+RAM+battery",
    0x19: "MBC5", 0x1A: "MBC5+RAM", 0x1B: "MBC5+RAM+battery",
    0x1C: "MBC5+rumble", 0x1D: "MBC5+rumble+RAM", 0x1E: "MBC5+rumble+RAM+battery",
    0x20: "MBC6", 0x22: "MBC7+sensor+rumble+RAM+battery",
    0xFC: "Pocket Camera", 0xFD: "Bandai TAMA5", 0xFE: "HuC3", 0xFF: "HuC1+RAM+battery",
}
DESTINATION = {0x00: "Japan", 0x01: "elsewhere"}
# Where the CPU starts looking: the RST table, the interrupt vectors and the cartridge entry point.
VECTORS = tuple(range(0x00, 0x40, 0x08)) + (0x40, 0x48, 0x50, 0x58, 0x60)
ENTRY = 0x100


def header_checksum(data: bytes) -> int:
    """The header checksum over 0x134-0x14C (the boot ROM verifies this; so do we)."""
    return (-(sum(data[0x134:0x14D]) + 0x19)) & 0xFF


def is_gb(data: bytes) -> bool:
    """A Game Boy or Game Boy Color ROM: the Nintendo logo, a matching header checksum, and a size that
    is a power-of-two number of 16 KiB banks (32 KiB … 8 MiB). Deliberately strict: a false positive
    would hand a random binary to the SM83 lane."""
    if len(data) < HEADER_LEN or data[0x104:0x134] != LOGO:
        return False
    if data[0x14D] != header_checksum(data):
        return False
    size = len(data)
    special_sizes = {ROM_SIZES[code] for code in (0x52, 0x53, 0x54)}
    if size < 0x8000 or size > 0x800000 or ((size & (size - 1)) and size not in special_sizes):
        return False
    return True


def read_info(data: bytes) -> dict:
    """The cartridge header, in the shape the rest of the fork expects from a ROM reader."""
    if not is_gb(data):
        raise RomError("this isn't a Game Boy or Game Boy Color ROM")
    cgb = data[0x143]
    platform = "gbc" if cgb & 0x80 else "gb"
    title_bytes = data[0x134:0x143] if cgb & 0x80 else data[0x134:0x144]
    title = title_bytes.split(b"\0", 1)[0].decode("ascii", "replace").strip()
    size_code, ram_code, cart = data[0x148], data[0x149], data[0x147]
    declared = ROM_SIZES.get(size_code)
    return {
        "title": title or "Game Boy ROM",
        "game_code": "",  # Game Boy cartridges have no game code field (that arrived with the GBA)
        "region": DESTINATION.get(data[0x14A], "unknown"),
        "version": data[0x14C],
        "size": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
        "entrypoint": ENTRY,
        "platform": platform,
        "byte_order": "little",
        "compiler_guess": "RGBDS or SDCC (unknown)",
        "gp": 0,
        "cgb": {"flag": f"0x{cgb:02x}", "mode": "cgb-only" if cgb == 0xC0 else "cgb-enhanced" if cgb & 0x80 else "dmg"},
        "cartridge": MBC.get(cart, f"unknown (0x{cart:02x})"),
        "mbc": MBC.get(cart, f"unknown (0x{cart:02x})"),
        "rom_banks": len(data) // BANK_SIZE,
        "rom_bytes_declared": declared,
        "size_mismatch": bool(declared and declared != len(data)),
        "ram_bytes": RAM_SIZES.get(ram_code, 0),
        "header_checksum": f"0x{data[0x14D]:02x}",
        "global_checksum": f"0x{struct.unpack_from('>H', data, 0x14E)[0]:04x}",
        "super_game_boy": bool(data[0x146] == 0x03),
    }


def banks(data: bytes) -> list[bytes]:
    """The ROM as 16 KiB banks (the last one zero-padded when the size code is not a power of two)."""
    return [data[i:i + BANK_SIZE].ljust(BANK_SIZE, b"\0") for i in range(0, len(data), BANK_SIZE)]


def candidates(data: bytes, limit: int = 4000) -> list[tuple[int, str]]:
    """(flat offset, how it was found) for every plausible function start: the RST/interrupt vectors,
    the entry point, and every `call nn` / `jp nn` target that lands inside the ROM.

    Found two ways on purpose: a sequential sweep with the real decoder (alignment-safe, misses calls
    after a data blob), plus a byte scan for 0xCD/0xC3 (catches calls inside regions the sweep walked
    as data). The union is what `luvdis --min-calls 1` does for the GBA lane."""
    found: dict[int, str] = {}
    for addr in VECTORS:
        if addr < len(data):
            found.setdefault(addr, "vector")
    if ENTRY < len(data):
        found.setdefault(ENTRY, "entry")

    for off, _text, _ln in sm83.disassemble(data):
        bank = bank_of(off)
        for target, kind in ((sm83.call_target(data, off), "call"), (sm83.jump_target(data, off), "jp")):
            if target is None:
                continue
            flat = flat_offset(target, bank)  # the instruction holds a CPU address for its own bank
            if 0 <= flat < len(data):
                found.setdefault(flat, kind)
        if len(found) >= limit:
            break
    if len(found) < limit:  # byte-level pass for calls the sweep could not reach
        for off in range(len(data) - 2):
            if data[off] in (0xCD, 0xC3):
                target = data[off + 1] | (data[off + 2] << 8)
                flat = flat_offset(target, bank_of(off))
                if 0 <= flat < len(data):
                    found.setdefault(flat, "call" if data[off] == 0xCD else "jp")
            if len(found) >= limit:
                break
    return sorted(found.items())


@dataclass
class GbFunction:
    """One discovered function: where it is, its bytes, and its disassembly.

    `vram` is the **CPU address** (what the ROM's instructions refer to), `rom` the **flat offset** in
    the file, and `bank` the 16 KiB bank it lives in. See the module docstring."""

    name: str
    vram: int          # CPU address (bank 0: 0x0000-0x3FFF; other banks: 0x4000-0x7FFF)
    rom: int           # flat ROM offset
    code: bytes
    n_insns: int
    asm: str
    refs: list = field(default_factory=list)   # (offset, kind, address): 10 = call, as the GBA lane uses
    asm_only: bool = False
    bank: int = 0      # the 16 KiB bank the function lives in
    edges: dict = field(default_factory=dict)  # offset -> ROM offset: where a call or jump into the window went

    @property
    def size(self) -> int:
        return len(self.code)


def refs_of(code: bytes, addr: int, bank: int, limit: int, flat_start: int | None = None) -> list[tuple[int, int, int]]:
    """(offset_in_function, kind, CPU address) for every call/jump edge out of `code`.

    Kind 10 is a call edge, the same label the GBA lane uses, so `xrefs` builds the call graph from it.
    `limit` is the ROM size: a target outside it is not a reference. `flat_start` distinguishes the
    physical start from the CPU address for banked code; without it, the start is assumed to be `addr`."""
    out: list[tuple[int, int, int]] = []
    flat_start = addr if flat_start is None else flat_start
    for off, text, _ln in sm83.disassemble(code, addr):
        if not text.startswith(("call ", "jp ")) or "$" not in text:
            continue
        target = int(text.rsplit("$", 1)[1], 16)  # a CPU address
        flat = flat_offset(target, bank)
        if 0 <= flat < limit and not (flat_start <= flat < flat_start + len(code)):
            out.append((off - addr, 10, target))
    return out


def _extent(data: bytes, addr: int, next_addr: int, max_len: int = 0x400) -> bytes:
    """A function's bytes: from its start **up to and including** its terminating instruction, or to the
    next known start when it runs into one first (never past `max_len`).

    Exact extents matter for matching: a candidate compiled from C produces the function and nothing
    else, so padding the target with the bytes that follow it would make an otherwise perfect match
    impossible. Data that follows a function belongs to whatever the next candidate is."""
    hard = min(next_addr, addr + max_len, len(data))
    off = addr
    while off < hard:
        text, ln = sm83.decode(data[off:], off)
        off += max(1, ln)
        if sm83.is_return(text):
            return data[addr:off]
    return data[addr:hard]


def functions(data: bytes, log=lambda *_: None) -> list[GbFunction]:
    """Every discovered function, disassembled. Names spell the flat offset (`func_000123`)."""
    found = candidates(data)
    if not found:
        raise RomError("no code found in this Game Boy ROM: every candidate address pointed outside it")
    if len(found) >= 4000:
        log("the built-in sweep stopped at its limit of 4000 candidates, so functions after that point are missing")
    out: list[GbFunction] = []
    for i, (addr, _how) in enumerate(found):
        nxt = found[i + 1][0] if i + 1 < len(found) else min(addr + 0x400, len(data))
        if nxt <= addr:
            continue
        code = _extent(data, addr, nxt)
        insns = list(sm83.disassemble(code, addr))
        if not insns:
            continue
        refs = refs_of(code, cpu_address(addr), bank_of(addr), len(data), flat_start=addr)
        out.append(GbFunction(name=function_name(addr), vram=cpu_address(addr), rom=addr,
                              bank=bank_of(addr), code=code, n_insns=len(insns),
                              asm=sm83.asm_text(code, cpu_address(addr), width=4), refs=refs))
    log(f"found {len(out)} functions in {len(data) // BANK_SIZE} banks")
    return out


def split(path: Path, workdir: Path, log=lambda *_: None, prefer_mgbdis: bool = True,
          report: dict | None = None, hints: dict | None = None) -> list[GbFunction]:
    """A ROM file's functions.

    Preferred: **mgbdis + rgbds** (rom/gb_mgbdis), a real disassembler whose output reassembles the ROM byte for
    byte (the check that the split is faithful), told the game's code from its data by following the code
    (rom/gb_trace). Falls back to the built-in call-graph sweep when those tools are not installed, or when asked not
    to use them. `report`, if given, gets {"splitter": "mgbdis" | "built-in"} (the sweep is approximate, and the
    caller records that) and the regions that aren't code (`regions`); `hints` are corrections (rom/gb_trace)."""
    data = Path(path).read_bytes()
    if not is_gb(data):
        raise RomError("this isn't a Game Boy or Game Boy Color ROM")
    info = read_info(data)
    log(f"{info['platform'].upper()}: {info['title']} ({info['cartridge']}, {info['rom_banks']} banks)")
    if prefer_mgbdis:
        from . import gb_mgbdis

        if gb_mgbdis.available():
            try:
                fns, meta = gb_mgbdis.split(path, workdir, log=log, hints=hints)
                if meta["faithful"]:
                    log(f"split by mgbdis: {meta['functions']} functions, reassembly byte-identical")
                    if report is not None:
                        report["splitter"] = "mgbdis"
                        report["regions"] = meta.get("regions") or []
                    return fns
                log(f"mgbdis split did NOT rebuild the ROM byte for byte ({meta['rebuilt_bytes']} vs "
                    f"{meta['original_bytes']} bytes): using the built-in split instead")
            except Exception as e:  # a tool failure must never cost the user the import
                log(f"mgbdis split unavailable ({type(e).__name__}: {e}): using the built-in split")
        else:
            log("mgbdis/rgbds are not installed: using the built-in call-graph split")
    if report is not None:
        report["splitter"] = "built-in"
    log("WARNING: this ROM was split with the built-in sweep, which is approximate. On one real 1 MiB game about half of "
        "the functions it found were real and a quarter of the real ones were missing. Install mgbdis and rgbds "
        "(Setup lists them), then import the game again for the accurate split.")
    return functions(data, log=log)
