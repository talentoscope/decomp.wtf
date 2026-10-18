"""The Game Boy's hardware by name: hardware.inc 6.0 (the community's file, CC0, vendored in gbhw/) for its registers,
the cartridge mapper's registers and its memory areas, so listings say `ldh [rLCDC], a` and `ld hl, TILEMAP0` instead
of addresses, and documented assembly can use the same names (both hardware.inc and hardware_compat.inc are included
when it's assembled, so the older names work too).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).with_name("gbhw")
_DEF = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s+equ\s+(.+?)\s*(?:;.*)?$", re.IGNORECASE)
# memory areas a listing names a constant after (the first address of each)
AREAS = ("TILEMAP0", "TILEMAP1", "OAMRAM")


def include_dir() -> Path:
    return HERE


@lru_cache(maxsize=1)
def _defs() -> dict[str, int]:
    """name -> value for every `def NAME equ $XXXX` (or a name defined before it) in hardware.inc."""
    out: dict[str, int] = {}
    for line in (HERE / "hardware.inc").read_text(encoding="utf-8").splitlines():
        m = _DEF.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if (h := re.fullmatch(r"\$([0-9A-Fa-f]+)", value)):
            out[m.group(1)] = int(h.group(1), 16)
        elif value in out:
            out[m.group(1)] = out[value]
    return out


@lru_cache(maxsize=1)
def registers() -> dict[int, str]:
    """I/O address -> its register's name ($FF00-$FFFF; the first name hardware.inc gives an address)."""
    out: dict[int, str] = {}
    for name, value in _defs().items():
        if name.startswith("r") and 0xFF00 <= value <= 0xFFFF:
            out.setdefault(value, name)
    return out


# Register names from before hardware.inc 6.0 (mgbdis still prints them; hardware_compat.inc keeps only 5.x's), by the
# address they name: 6.0 calls the same register as registers() does.
OLD_REGISTERS = {
    "rP1": 0xFF00, "rNR10": 0xFF10, "rNR11": 0xFF11, "rNR12": 0xFF12, "rNR13": 0xFF13, "rNR14": 0xFF14,
    "rNR21": 0xFF16, "rNR22": 0xFF17, "rNR23": 0xFF18, "rNR24": 0xFF19, "rNR30": 0xFF1A, "rNR31": 0xFF1B,
    "rNR32": 0xFF1C, "rNR33": 0xFF1D, "rNR34": 0xFF1E, "rNR41": 0xFF20, "rNR42": 0xFF21, "rNR43": 0xFF22,
    "rNR44": 0xFF23, "rNR50": 0xFF24, "rNR51": 0xFF25, "rNR52": 0xFF26, "rKEY0": 0xFF4C, "rKEY1": 0xFF4D,
    "rHDMA1": 0xFF51, "rHDMA2": 0xFF52, "rHDMA3": 0xFF53, "rHDMA4": 0xFF54, "rHDMA5": 0xFF55, "rBCPS": 0xFF68,
    "rBCPD": 0xFF69, "rOCPS": 0xFF6A, "rOCPD": 0xFF6B, "rSVBK": 0xFF70, "rSMBK": 0xFF70,
}


@lru_cache(maxsize=1)
def old_names() -> dict[str, str]:
    """An older register name -> the 6.0 name for the same register."""
    regs = registers()
    return {old: regs[addr] for old, addr in OLD_REGISTERS.items() if addr in regs and regs[addr] != old}


def old_names_inc() -> str:
    """RGBDS definitions of the older names, for assembling code written with them."""
    return "".join(f"if !def({old})\ndef {old} equ {new}\nendc\n" for old, new in sorted(old_names().items()))


def mapper_registers(mapper: str) -> dict[int, str]:
    """The cartridge's own registers by the address games write them at (rom/gb_trace.mapper's names)."""
    defs = _defs()
    if mapper == "mbc5":
        names = ("rRAMG", "rROMB0", "rROMB1", "rRAMB")
    elif mapper == "mbc1":
        names = ("rRAMG", "rROMB", "rRAMB", "rBMODE")
    else:
        names = ("rRAMG", "rROMB", "rRAMB")
    return {defs[n]: n for n in names if n in defs}


def area(value: int) -> str | None:
    """The memory area a 16-bit constant is the start of (TILEMAP0...), else None."""
    defs = _defs()
    return next((n for n in AREAS if defs.get(n) == value), None)


def name_listing(text: str, mapper: str = "mbc5") -> str:
    """A listing in RGBDS brackets with its registers named: `[$ff40]` and `[$ff00+$40]` become `[rLCDC]`, an older
    name (rKEY1) its 6.0 one (rSPD), a write to the mapper `[rROMB0]`, and a constant that's a register or the start of
    a memory area gets a comment saying so."""
    regs, cart, old = registers(), mapper_registers(mapper), old_names()

    def mem(m: re.Match) -> str:
        addr = int(m.group(1), 16)
        name = regs.get(addr) or regs.get(0xFF00 + addr) if len(m.group(1)) <= 2 else regs.get(addr) or cart.get(addr)
        return f"[{name}]" if name else m.group(0)

    out = []
    for line in text.splitlines():
        code, sep, comment = line.partition(";")
        code = re.sub(r"[\[(]\$ff00\+\$([0-9a-fA-F]{2})[\])]", lambda m: f"[$ff{m.group(1)}]", code)
        code = re.sub(r"\(\$([0-9a-fA-F]{4})\)", lambda m: f"[${m.group(1)}]", code)
        code = re.sub(r"\[\$([0-9a-fA-F]{4})\]", mem, code)
        code = re.sub(r"\b(r[A-Z][A-Z0-9_]*)\b", lambda m: old.get(m.group(1), m.group(1)), code)
        if not sep and (k := re.search(r"\bld (?:bc|de|hl|sp), \$([0-9a-fA-F]{4})\b", code)):
            value = int(k.group(1), 16)
            what = regs.get(value) or area(value)
            if what:
                code, sep, comment = code.rstrip(), " ; ", what
        out.append(code + sep + comment)
    return "\n".join(out)


def mapper_of(cartridge: str) -> str:
    """rom/gb_trace.mapper's name for a cartridge type as the header reader spells it ("MBC5+RAM+battery")."""
    text = (cartridge or "").upper()
    return next((m.lower() for m in ("MBC1", "MBC2", "MBC3", "MBC5") if text.startswith(m)), "mbc5")
