"""The NES's hardware, by the names the assembly track uses (rom/nes_asm.py): the PPU's and the APU's registers as the
nesdev wiki names them (PPUCTRL, OAMDMA, SQ1_VOL...), the joypads, and the registers of the mappers that have
named ones (MMC1, MMC3). A mapper written anywhere in its range (UxROM, CNROM, AxROM...) has none: what such a game
writes to is a table of its own (to avoid bus conflicts), which is named as the game's data is.
"""

from __future__ import annotations

REGISTERS = {
    0x2000: "PPUCTRL", 0x2001: "PPUMASK", 0x2002: "PPUSTATUS", 0x2003: "OAMADDR", 0x2004: "OAMDATA",
    0x2005: "PPUSCROLL", 0x2006: "PPUADDR", 0x2007: "PPUDATA",
    0x4000: "SQ1_VOL", 0x4001: "SQ1_SWEEP", 0x4002: "SQ1_LO", 0x4003: "SQ1_HI",
    0x4004: "SQ2_VOL", 0x4005: "SQ2_SWEEP", 0x4006: "SQ2_LO", 0x4007: "SQ2_HI",
    0x4008: "TRI_LINEAR", 0x400A: "TRI_LO", 0x400B: "TRI_HI",
    0x400C: "NOISE_VOL", 0x400E: "NOISE_LO", 0x400F: "NOISE_HI",
    0x4010: "DMC_FREQ", 0x4011: "DMC_RAW", 0x4012: "DMC_START", 0x4013: "DMC_LEN",
    0x4014: "OAMDMA", 0x4015: "SND_CHN", 0x4016: "JOY1", 0x4017: "JOY2",
}
SHARED = {"APU_FRAME": 0x4017}  # $4017: the second joypad when read, the APU's frame counter when written
WRITTEN = {0x4017: "APU_FRAME"}
MMC1 = {0x8000: "MMC1_CONTROL", 0xA000: "MMC1_CHR0", 0xC000: "MMC1_CHR1", 0xE000: "MMC1_PRG"}
MMC3 = {0x8000: "MMC3_BANK_SELECT", 0x8001: "MMC3_BANK_DATA", 0xA000: "MMC3_MIRRORING", 0xA001: "MMC3_PRG_RAM",
        0xC000: "MMC3_IRQ_LATCH", 0xC001: "MMC3_IRQ_RELOAD", 0xE000: "MMC3_IRQ_DISABLE", 0xE001: "MMC3_IRQ_ENABLE"}
_MAPPERS = {1: MMC1, 4: MMC3, 118: MMC3, 119: MMC3, 206: MMC3}


def mapper_registers(mapper: int) -> dict[int, str]:
    return _MAPPERS.get(mapper, {})


def names(mapper: int = -1) -> dict[str, int]:
    """Every hardware name -> its address (with the mapper's registers when it has named ones)."""
    out = {n: a for a, n in REGISTERS.items()} | SHARED
    return out | {n: a for a, n in mapper_registers(mapper).items()}


def name_of(address: int, write: bool, mapper: int = -1) -> str | None:
    """The hardware's name for an address an instruction reads or writes (a register, or one plus an offset: MMC1's
    serial port is written anywhere in its range), else None."""
    if write and address in WRITTEN:
        return WRITTEN[address]
    if address in REGISTERS:
        return REGISTERS[address]
    if 0x2008 <= address < 0x4000:  # (the PPU's registers, mirrored every 8 bytes)
        return f"{REGISTERS[0x2000 + address % 8]} + ${address - 0x2000 - address % 8:04X}"
    regs = mapper_registers(mapper)
    if write and address >= 0x8000 and regs:
        if address in regs:
            return regs[address]
        if regs is MMC1:
            base = address & 0xE000
            return f"{MMC1[base]} + ${address - base:04X}"
        base = address & 0xE001
        return f"{MMC3[base]} + ${address - base:04X}" if base in MMC3 else None
    return None


def hardware(address: int, write: bool, mapper: int = -1) -> bool:
    """Whether an operand addresses the hardware (checked by moving its names), not the game's own memory."""
    return 0x2000 <= address < 0x4020 or (write and address >= 0x8000 and bool(mapper_registers(mapper)))


def inc(mapper: int = -1, moved: bool = False) -> str:
    """The names as ca65 equates. `moved`: each one off by one (the judge's hardware test)."""
    return "".join(f"{name} = ${(v + 1 if moved else v) & 0xFFFF:04X}\n" for name, v in sorted(names(mapper).items()))
