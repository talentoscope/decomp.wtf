"""One function's bytes on the banked lanes (Game Boy, Master System / Game Gear, NES, and the C64's one 64 KiB of
RAM, a single bank), as the project stores them:
its instructions, its stored listing, the calls and jumps out of it, and its automatic name. What cutting a function
in two, or joining two, needs to rebuild each piece the way the import made it (rom/gb.py, rom/sms.py, rom/nes.py).

A function is addressed by its ROM offset (`flat`: a file offset for the Game Boy and NES, an offset in the copier-
header-free image for the Master System, as each lane records `rom_offset`) and the CPU address it runs at.
"""

from __future__ import annotations

from . import gb, mos6502, nes, sm83, sms, z80
from .sms_mapping import BusState


class Lane:
    """The banked lane a project's CPU uses, with the project's ROM facts (`cfg["rom"]`)."""

    def __init__(self, arch: str, rom: dict):
        self.arch, self.rom = arch, rom or {}
        self.size = int(self.rom.get("size") or 0) or 1 << 23
        self.cpu_mod = {"sm83": sm83, "z80": z80, "mos6502": mos6502, "6510": mos6502}[arch]
        if arch == "mos6502":
            self.prg_off = int(self.rom.get("prg_offset") or 0)
            self.layout = nes.Layout(nes.layout_of({"mapper": self.rom.get("mapper", 0),
                                                    "prg_size": self.rom.get("prg_size", 0)}).kind,
                                     int(self.rom.get("bank_size") or nes.PRG_UNIT), int(self.rom.get("prg_size") or 0))

    # -- where a byte is

    def bank(self, flat: int) -> int:
        if self.arch == "6510":  # one RAM: a function's offset is HEADER + its address (rom/c64.py)
            return 0
        if self.arch == "sm83":
            return gb.bank_of(flat)
        if self.arch == "z80":
            return sms.bank_of(flat)
        return self.layout.bank_of(flat - self.prg_off)

    def name(self, flat: int, cpu: int | None = None) -> str:
        """The automatic name the import gives a function starting at `flat` (running at `cpu`, where the Master
        System lane found its bank mapped)."""
        if self.arch == "6510":
            from . import c64
            return f"func_{(cpu if cpu is not None else flat - c64.HEADER) & 0xFFFF:04x}"
        if self.arch == "sm83":
            return gb.function_name(flat)
        if self.arch == "z80":
            return sms.function_name(flat, self.size, cpu)
        return nes.function_name(flat - self.prg_off, self.layout)

    def flat_of(self, cpu: int, from_flat: int, from_cpu: int | None = None, *,
                mapping: BusState | None = None) -> int | None:
        """The ROM offset a call or jump to `cpu` reaches from code at `from_flat` (running at `from_cpu`, when
        known): None for RAM, registers, unmapped."""
        if self.arch == "6510":
            from . import c64
            return c64.HEADER + cpu if 0 <= cpu <= 0xFFFF else None
        bank = self.bank(from_flat)
        if self.arch == "sm83":
            return gb.flat_offset(cpu, bank) if cpu < 0x8000 else None
        if self.arch == "z80":
            return sms.target_flat(cpu, bank, self.size, from_cpu, mapping=mapping)
        p = self.layout.target(cpu, bank)
        return None if p is None else p + self.prg_off

    # -- what the bytes are

    def insns(self, code: bytes, cpu: int) -> list[tuple[int, str, int]]:
        """(CPU address, text, length) per instruction."""
        return list(self.cpu_mod.disassemble(code, cpu))

    def listing(self, code: bytes, cpu: int) -> str:
        if self.arch == "6510":  # with the C64's chip registers named, as the import lists them
            from . import c64_hw
            return c64_hw.name_listing(mos6502.asm_text(code, cpu, width=4))
        return self.cpu_mod.asm_text(code, cpu, width=4)

    def refs(self, code: bytes, cpu: int, flat: int) -> list[list[int]]:
        """[offset in the function, 10, CPU address] per call or jump out of it, as the import records them."""
        if self.arch == "6510":
            from . import c64
            return [list(r) for r in c64.refs_of(code, cpu)]
        bank = self.bank(flat)
        if self.arch == "sm83":
            out = gb.refs_of(code, cpu, bank, self.size)
        elif self.arch == "z80":
            out = sms.refs_of(code, cpu, bank, self.size)
        else:
            out = nes.refs_of(code, cpu, bank, self.layout)
        return [list(r) for r in out]

    def flow(self, text: str) -> tuple[str, int | None]:
        """How an instruction moves control: ret | jump (both end straight-line flow) | cret | cjump | call | plain,
        and its target's CPU address when it has one."""
        return self.cpu_mod.flow(text)

    def falls_through(self, code: bytes, cpu: int) -> bool:
        """True when the last instruction of `code` doesn't end its flow: the bytes after it run next."""
        insns = self.insns(code, cpu)
        return bool(insns) and self.flow(insns[-1][1])[0] not in ("ret", "jump")

    def branches_into(self, code: bytes, cpu: int, lo: int, hi: int) -> bool:
        """True when a jump (not a call) in `code` goes to a CPU address in [lo, hi)."""
        for _a, text, _ln in self.insns(code, cpu):
            kind, target = self.flow(text)
            if kind in ("jump", "cjump") and target is not None and lo <= target < hi:
                return True
        return False
