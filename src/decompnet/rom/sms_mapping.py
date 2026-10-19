"""SMS/GG cartridge address resolution from explicit bus facts.

CPU addresses, physical cartridge offsets and assembler section geometry are distinct.
Reset state is an explicit choice: the fully unknown state never invents cartridge banks.
References: SMS Power's MemoryMap and Mappers development pages. Hardware-specific or
undeclared wiring remains unresolved; this module does not emulate RAM contents or a BIOS.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

BANK = 0x4000
REGISTERS = {0xFFFD: 0, 0xFFFE: 1, 0xFFFF: 2}


def bank_mask(size: int) -> int:
    banks = max(1, (size + BANK - 1) // BANK)
    return (1 << (banks - 1).bit_length()) - 1


def wram_address(cpu: int) -> int | None:
    """Canonical alias if the selected source is work RAM; this does not establish that selection."""
    return 0xC000 + (cpu & 0x1FFF) if 0xC000 <= cpu <= 0xFFFF else None


@dataclass(frozen=True)
class Address:
    cpu: int
    space: str
    offset: int | None = None
    reason: str = ""
    bank: int | None = None

    @property
    def rom_offset(self) -> int | None:
        return self.offset if self.space == "rom" else None


@dataclass(frozen=True)
class BusState:
    # Effective ROM banks and raw register selections differ when bank shifting is active.
    banks: tuple[int | None, ...] = (None, None, None)
    registers: tuple[int | None, ...] = (None, None, None)
    control: int | None = None
    cartridge: bool | None = None
    bios: bool | None = None
    wram: bool | None = None
    other_source: bool | None = None
    rom_modified: bool = False

    def __post_init__(self):
        for values in (self.banks, self.registers):
            if not isinstance(values, tuple) or len(values) != 3 or any(v is not None and (type(v) is not int or not 0 <= v <= 255) for v in values):
                raise ValueError("a mapping state has three byte-sized bank selections or unknown values")
        if self.control is not None and (type(self.control) is not int or not 0 <= self.control <= 255):
            raise ValueError("mapper control is a byte or unknown")
        if any(v is not None and type(v) is not bool for v in (self.cartridge, self.bios, self.wram, self.other_source)):
            raise ValueError("bus selections are true, false or unknown")
        if type(self.rom_modified) is not bool:
            raise ValueError("modified cartridge state is a boolean")

    @classmethod
    def cartridge_only(cls, banks=(None, None, None), *, control=0, registers=(None, None, None)):
        """An explicitly cartridge-only analysis context, not an assertion about every console boot."""
        return cls(tuple(banks), tuple(registers), control, True, False, True, False)


@dataclass(frozen=True)
class Mapper:
    kind: str
    size: int

    def __post_init__(self):
        if self.kind not in ("sega", "mapperless"):
            raise ValueError("this mapping model supports Sega and mapperless cartridges")
        if type(self.size) is not int or not 0x2000 <= self.size <= 0x400000 or self.size % 0x2000:
            raise ValueError("invalid SMS/GG cartridge size")
        if self.kind == "mapperless" and self.size > 3 * BANK:
            raise ValueError("a mapperless cartridge cannot expose more than 48 KiB")

    def reset(self) -> BusState:
        """The 315-5235/emulator power-up recipe; callers must choose it explicitly."""
        banks = tuple(bank & bank_mask(self.size) for bank in (0, 1, 2))
        return BusState.cartridge_only(banks, registers=(0, 1, 2))

    def resolve(self, cpu: int, state: BusState) -> Address:
        if type(cpu) is not int or not 0 <= cpu <= 0xFFFF:
            return Address(-1, "unknown", reason="outside the 16-bit CPU address space")
        if cpu >= 0xC000:
            if state.other_source is not False:
                return Address(cpu, "unknown", reason="other device/RAM selection is unresolved")
            high_ram = False if self.kind == "mapperless" or state.cartridge is False else \
                bool(state.control & 0x10) if state.cartridge is True and state.control is not None else None
            if high_ram is True:
                if state.wram is not False:
                    return Address(cpu, "unknown", reason="cartridge RAM may contend with system RAM")
                return Address(cpu, "cart_ram", reason="upper cartridge RAM wiring is not declared")
            if high_ram is None or state.wram is None:
                return Address(cpu, "unknown", reason="upper RAM source is unresolved")
            return Address(cpu, "wram", cpu & 0x1FFF) if state.wram else Address(cpu, "unmapped", reason="RAM disabled")
        if state.other_source is not False or state.bios is None or state.cartridge is None:
            return Address(cpu, "unknown", reason="cartridge/BIOS/device selection is unresolved")
        if state.bios:
            if state.cartridge:
                return Address(cpu, "unknown", reason="BIOS and cartridge may both drive the bus")
            return Address(cpu, "bios", reason="BIOS selected; its layout is not declared")
        if not state.cartridge:
            return Address(cpu, "unmapped", reason="cartridge disabled")
        if state.rom_modified:
            return Address(cpu, "unknown", reason="cartridge contents may have been modified")
        if self.kind == "mapperless":
            flat, bank = cpu, cpu // BANK
        elif cpu < 0x0400:
            flat, bank = cpu, 0
        else:
            if cpu >= 0x8000:
                if state.control is None:
                    return Address(cpu, "unknown", reason="slot 2 RAM selection is unresolved")
                if state.control & 8:
                    return Address(cpu, "cart_ram", reason="cartridge RAM size/wiring is not declared", bank=(state.control >> 2) & 1)
            bank = state.banks[cpu // BANK]
            if bank is None:
                return Address(cpu, "unknown", reason="ROM bank is unresolved")
            flat = bank * BANK + (cpu & (BANK - 1))
        if flat >= self.size:
            return Address(cpu, "unknown", reason="address exceeds supplied ROM; mirror wiring is not declared")
        return Address(cpu, "rom", flat, bank=bank)

    def write(self, state: BusState, cpu: int, value: int | None) -> BusState:
        if type(cpu) is not int or not 0 <= cpu <= 0xFFFF:
            raise ValueError("a mapper write requires a 16-bit CPU address")
        if value is not None and (type(value) is not int or not 0 <= value <= 255):
            raise ValueError("a mapper write is a byte or an unknown value")
        if self.kind == "mapperless":
            return state
        if cpu == 0xFFFC or cpu in REGISTERS:
            if state.cartridge is not True:
                return replace(state, banks=(None, None, None), registers=(None, None, None), control=None)
            if cpu == 0xFFFC:
                return replace(state, control=value)  # bank shifting takes effect on the next bank-register write
            registers = list(state.registers)
            registers[REGISTERS[cpu]] = value
            shift = None if state.control is None else (0, 0x18, 0x10, 8)[state.control & 3]
            if shift and self.size != 0x80000:
                shift = None  # documented only for the 512 KiB mapper variant
            banks = tuple((v + shift) & bank_mask(self.size) if v is not None and shift is not None else None for v in registers)
            return replace(state, banks=banks, registers=tuple(registers))
        if 0 <= cpu < 0xC000 and state.cartridge is not False and (state.control is None or state.control & 0x80):
            return replace(state, rom_modified=True)
        return state


def entry_state(flat: int, cpu: int, size: int) -> BusState:
    """Conditional ROM entry evidence establishes its own window, never the other pageable windows."""
    banks: list[int | None] = [None, None, None]
    if 0 <= flat < size and 0 <= cpu < 0xC000 and flat % BANK == cpu % BANK and cpu >= 0x0400:
        banks[cpu // BANK] = flat // BANK
    return BusState.cartridge_only(banks)
