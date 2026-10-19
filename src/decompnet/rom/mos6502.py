"""A MOS 6502 decoder for the NES's CPU (the Ricoh 2A03: a 6502 without decimal mode), in the same shape as the fork's
Z80 and SM83 decoders (`z80.py`, `sm83.py`): `decode`, `disassemble`, `asm_text`, `flow`, and a capstone-shaped `Decoder`
for the scoring pipeline.

Why not capstone's MOS65XX back end? The sweep that finds functions in a ROM decodes every candidate address, and a
table lookup in Python is far cheaper than a capstone call per instruction; the text it produces is also the one the
whole fork stores and shows, so both the discovery and the scoring read the same instruction forms. The test suite
decodes all 256 opcodes with this module and with capstone and requires the two to agree on length, mnemonic and
operand value, which is what keeps this table honest.

Text. Hexadecimal is `$`-prefixed like the Z80 decoder's, a zero-page operand has two digits and an absolute one four
(`lda $10`, `lda $0010`: the two assemble to different instructions), and a branch shows its absolute target
(`bne $8005`), so the stored assembly reads the way the ROM does. The 105 opcodes the 6502 leaves undefined (some
games use them) decode as `db $xx`, one byte each, which keeps a sweep over the ROM in step.
"""

from __future__ import annotations

# mode -> (instruction length, operand format)
_MODES = {
    "imp": 1, "acc": 1, "imm": 2, "zp": 2, "zpx": 2, "zpy": 2, "izx": 2, "izy": 2, "rel": 2,
    "abs": 3, "abx": 3, "aby": 3, "ind": 3,
}

# mnemonic -> {mode: opcode}: the 151 documented NMOS 6502 instructions.
_TABLE = {
    "adc": dict(imm=0x69, zp=0x65, zpx=0x75, abs=0x6D, abx=0x7D, aby=0x79, izx=0x61, izy=0x71),
    "and": dict(imm=0x29, zp=0x25, zpx=0x35, abs=0x2D, abx=0x3D, aby=0x39, izx=0x21, izy=0x31),
    "asl": dict(acc=0x0A, zp=0x06, zpx=0x16, abs=0x0E, abx=0x1E),
    "bcc": dict(rel=0x90), "bcs": dict(rel=0xB0), "beq": dict(rel=0xF0),
    "bit": dict(zp=0x24, abs=0x2C),
    "bmi": dict(rel=0x30), "bne": dict(rel=0xD0), "bpl": dict(rel=0x10),
    "brk": dict(zp=0x00),  # two bytes: the CPU skips a signature byte after it
    "bvc": dict(rel=0x50), "bvs": dict(rel=0x70),
    "clc": dict(imp=0x18), "cld": dict(imp=0xD8), "cli": dict(imp=0x58), "clv": dict(imp=0xB8),
    "cmp": dict(imm=0xC9, zp=0xC5, zpx=0xD5, abs=0xCD, abx=0xDD, aby=0xD9, izx=0xC1, izy=0xD1),
    "cpx": dict(imm=0xE0, zp=0xE4, abs=0xEC),
    "cpy": dict(imm=0xC0, zp=0xC4, abs=0xCC),
    "dec": dict(zp=0xC6, zpx=0xD6, abs=0xCE, abx=0xDE),
    "dex": dict(imp=0xCA), "dey": dict(imp=0x88),
    "eor": dict(imm=0x49, zp=0x45, zpx=0x55, abs=0x4D, abx=0x5D, aby=0x59, izx=0x41, izy=0x51),
    "inc": dict(zp=0xE6, zpx=0xF6, abs=0xEE, abx=0xFE),
    "inx": dict(imp=0xE8), "iny": dict(imp=0xC8),
    "jmp": dict(abs=0x4C, ind=0x6C),
    "jsr": dict(abs=0x20),
    "lda": dict(imm=0xA9, zp=0xA5, zpx=0xB5, abs=0xAD, abx=0xBD, aby=0xB9, izx=0xA1, izy=0xB1),
    "ldx": dict(imm=0xA2, zp=0xA6, zpy=0xB6, abs=0xAE, aby=0xBE),
    "ldy": dict(imm=0xA0, zp=0xA4, zpx=0xB4, abs=0xAC, abx=0xBC),
    "lsr": dict(acc=0x4A, zp=0x46, zpx=0x56, abs=0x4E, abx=0x5E),
    "nop": dict(imp=0xEA),
    "ora": dict(imm=0x09, zp=0x05, zpx=0x15, abs=0x0D, abx=0x1D, aby=0x19, izx=0x01, izy=0x11),
    "pha": dict(imp=0x48), "php": dict(imp=0x08), "pla": dict(imp=0x68), "plp": dict(imp=0x28),
    "rol": dict(acc=0x2A, zp=0x26, zpx=0x36, abs=0x2E, abx=0x3E),
    "ror": dict(acc=0x6A, zp=0x66, zpx=0x76, abs=0x6E, abx=0x7E),
    "rti": dict(imp=0x40), "rts": dict(imp=0x60),
    "sbc": dict(imm=0xE9, zp=0xE5, zpx=0xF5, abs=0xED, abx=0xFD, aby=0xF9, izx=0xE1, izy=0xF1),
    "sec": dict(imp=0x38), "sed": dict(imp=0xF8), "sei": dict(imp=0x78),
    "sta": dict(zp=0x85, zpx=0x95, abs=0x8D, abx=0x9D, aby=0x99, izx=0x81, izy=0x91),
    "stx": dict(zp=0x86, zpy=0x96, abs=0x8E),
    "sty": dict(zp=0x84, zpx=0x94, abs=0x8C),
    "tax": dict(imp=0xAA), "tay": dict(imp=0xA8), "tsx": dict(imp=0xBA),
    "txa": dict(imp=0x8A), "txs": dict(imp=0x9A), "tya": dict(imp=0x98),
}

#: opcode -> (mnemonic, mode)
OPCODES: dict[int, tuple[str, str]] = {op: (m, mode) for m, modes in _TABLE.items() for mode, op in modes.items()}

BRANCHES = frozenset({"bcc", "bcs", "beq", "bmi", "bne", "bpl", "bvc", "bvs"})


def decode(data: bytes, addr: int) -> tuple[str, int]:
    """(text, length) for the instruction at the start of `data`, which sits at address `addr`.

    An undefined opcode, or an instruction cut short by the end of the buffer, decodes as one `db $xx`, so a sweep
    over the ROM never desynchronises."""
    if not data:
        return "db $00", 1
    op = data[0]
    known = OPCODES.get(op)
    if known is None:
        return f"db ${op:02x}", 1
    mnemonic, mode = known
    n = _MODES[mode]
    if len(data) < n:
        return f"db ${op:02x}", 1
    lo = data[1] if n > 1 else 0
    word = lo | (data[2] << 8) if n > 2 else lo
    if mode == "imp":
        return mnemonic, 1
    if mode == "acc":
        return f"{mnemonic} a", 1
    if mode == "imm":
        return f"{mnemonic} #${lo:02x}", 2
    if mode == "zp":
        return f"{mnemonic} ${lo:02x}", 2
    if mode == "zpx":
        return f"{mnemonic} ${lo:02x},x", 2
    if mode == "zpy":
        return f"{mnemonic} ${lo:02x},y", 2
    if mode == "izx":
        return f"{mnemonic} (${lo:02x},x)", 2
    if mode == "izy":
        return f"{mnemonic} (${lo:02x}),y", 2
    if mode == "rel":
        return f"{mnemonic} ${(addr + 2 + (lo - 256 if lo & 0x80 else lo)) & 0xFFFF:04x}", 2
    if mode == "abs":
        return f"{mnemonic} ${word:04x}", 3
    if mode == "abx":
        return f"{mnemonic} ${word:04x},x", 3
    if mode == "aby":
        return f"{mnemonic} ${word:04x},y", 3
    return f"{mnemonic} (${word:04x})", 3  # ind


def length(data: bytes, addr: int = 0) -> int:
    return decode(data, addr)[1]


def disassemble(data: bytes, base: int = 0, count: int | None = None, start: int = 0):
    """Yield (address, text, length) walking forward from `start`; stops at the end of the buffer or after `count`
    instructions. A byte that cannot be decoded is emitted as data and advances by one."""
    addr, off = base + start, start
    emitted = 0
    while off < len(data) and (count is None or emitted < count):
        text, ln = decode(data[off:off + 3], addr)
        yield addr, text, ln
        off += max(1, ln)
        addr += max(1, ln)
        emitted += 1


def asm_text(code: bytes, base: int, width: int = 4, data: dict[int, int] | None = None) -> str:
    """The fork's stored asm form: `ADDR  mnemonic operands` per line. `data` ({address: length}): bytes in it that
    aren't instructions (a table after a call to a jump engine), shown as `db`."""
    if not data:
        return "\n".join(f"{addr:0{width}x}  {text}" for addr, text, _ in disassemble(code, base))
    out, off = [], 0
    while off < len(code):
        addr = base + off
        if (n := data.get(addr)):
            chunk = code[off:off + n]
            for k in range(0, len(chunk), 16):
                out.append(f"{addr + k:0{width}x}  db " + ", ".join(f"${b:02x}" for b in chunk[k:k + 16]))
            off += max(1, len(chunk))
            continue
        text, ln = decode(code[off:off + 3], addr)
        out.append(f"{addr:0{width}x}  {text}")
        off += max(1, ln)
    return "\n".join(out)


# ---------------------------------------------------------------- control flow

def flow(text: str) -> tuple[str, int | None]:
    """How an instruction moves control: (kind, target address or None).

    ret and jump end straight-line flow; cjump (a branch) and call fall through as well. `jmp ($xxxx)` leaves with no
    known target, and so does `brk`, which is what a run of zero bytes (padding, or data) decodes to."""
    mnemonic, _, ops = text.partition(" ")
    if mnemonic in ("rts", "rti", "brk"):
        return "ret", None
    if mnemonic == "jmp":
        if ops.startswith("("):
            return "ret", None
        return "jump", int(ops.lstrip("$"), 16)
    if mnemonic == "jsr":
        return "call", int(ops.lstrip("$"), 16)
    if mnemonic in BRANCHES:
        return "cjump", int(ops.lstrip("$"), 16)
    return "plain", None


def is_return(text: str) -> bool:
    """True for an instruction that ends a function: a return, or an unconditional jump (a tail call or the end of a
    loop body). A conditional branch falls through, so code follows it."""
    return flow(text)[0] in ("ret", "jump")


# ---------------------------------------------------------------- capstone-shaped adapter

_JUMPY = frozenset({"jmp", "jsr", "rts", "rti", "brk"}) | BRANCHES


class Insn:
    """The subset of capstone's instruction object the scoring pipeline reads (`id`, `group`, the operand text and the
    encoding), so a 6502 candidate travels the same path as every other architecture."""

    __slots__ = ("address", "size", "bytes", "mnemonic", "op_str", "id")

    def __init__(self, address: int, size: int, raw: bytes, mnemonic: str, op_str: str):
        self.address, self.size, self.bytes = address, size, raw
        self.mnemonic, self.op_str = mnemonic, op_str
        self.id = 0 if mnemonic == "db" else 1  # capstone renders undecodable bytes with id 0

    def group(self, _group) -> bool:
        return self.mnemonic in _JUMPY


class Decoder:
    """`capstone.Cs`-shaped 6502 decoder: `for ins in Decoder().disasm(code, addr)`."""

    version = "mos6502-1"

    def disasm(self, code: bytes, addr: int = 0):
        for address, text, size in disassemble(code, addr):
            mnemonic, _, op_str = text.partition(" ")
            yield Insn(address, size, code[address - addr:address - addr + size], mnemonic, op_str)
