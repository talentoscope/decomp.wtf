"""The Master System's and Game Gear's hardware, by the names the assembly track uses (rom/sms_asm.py).

The Game Boy has one set of names everyone uses (hardware.inc, gb_hw.py); these consoles don't, so this is a fixed
set: the I/O ports (the VDP, the PSG, the joypads, memory and I/O control, the FM unit, the Game Gear's start
button, link port and stereo) and the Sega mapper's registers, after what the consoles' technical documentation
calls them. A port that two names share (0x7F: the H counter when read, the PSG when written; 0xBF: the VDP's
status when read, its control port when written) has both.
"""

from __future__ import annotations

# port -> name; and the names that share a port with these
PORTS = {
    0x00: "PortGGStart", 0x01: "PortGGLinkData", 0x02: "PortGGLinkDirection", 0x03: "PortGGSerialOut",
    0x04: "PortGGSerialIn", 0x05: "PortGGSerialControl", 0x06: "PortGGStereo",
    0x3E: "PortMemoryControl", 0x3F: "PortIOControl",
    0x7E: "PortVCounter", 0x7F: "PortHCounter",
    0xBE: "PortVDPData", 0xBF: "PortVDPControl",
    0xDC: "PortIOA", 0xDD: "PortIOB",
    0xF0: "PortFMAddress", 0xF1: "PortFMData", 0xF2: "PortFMControl",
}
SHARED = {"PortPSG": 0x7F, "PortVDPStatus": 0xBF}
WRITTEN = {0x7F: "PortPSG"}  # the name a write to the port goes by
READ = {0xBF: "PortVDPStatus"}  # ... and a read
MAPPER = {0xFFFC: "MapperControl", 0xFFFD: "MapperSlot0", 0xFFFE: "MapperSlot1", 0xFFFF: "MapperSlot2"}


def names() -> dict[str, int]:
    """Every hardware name -> its port or address."""
    return {**{n: p for p, n in PORTS.items()}, **SHARED, **{n: a for a, n in MAPPER.items()}}


def port_name(port: int, write: bool) -> str | None:
    return (WRITTEN if write else READ).get(port) or PORTS.get(port)


def inc(moved: bool = False) -> str:
    """The names as WLA-DX definitions. `moved`: each one off by one (the judge's hardware test: an operand that is
    a register changes with it, a number left in doesn't)."""
    def value(n: int) -> int:
        if not moved:
            return n
        if n >= 0xFFFC:
            return n - 1 if n == 0xFFFF else n + 1
        return n - 1 if n == 0xFF else n + 1
    return "".join(f".DEFINE {name} ${value(v):0{4 if v > 0xFF else 2}X}\n" for name, v in sorted(names().items()))
