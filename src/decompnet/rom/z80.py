"""Zilog Z80 disassembler for Sega Master System and Game Gear ROMs.

Why this exists: the fork decodes candidate instructions with capstone for every other architecture, but
capstone has no Z80 back end. Like `sm83.py` for the Game Boy, SMS and Game Gear support brings its own decoder,
and it is deliberately complete: all base opcodes, the CB, ED, DD, FD and DDCB/FDCB prefixed ones, and the
undocumented ones (IXH/IXL/IYH/IYL halves, SLL, the register-copying DDCB forms, `in f,(c)`) decode too.
Opcodes the CPU does not define come back as data (`db $xx`) so a sweep never loses alignment.

Text style follows `sm83.py` (lower-case mnemonics, `$` hex): `ld a, $12` · `ld hl, ($c000)` · `jr nz, $0150` ·
`call $2000` · `ld a, (ix+$05)` · `out ($bf), a` · `djnz $0123` · `ex af, af'`. Relative jumps are shown as
the absolute address they reach.
"""

# Portions ported from SynapticDreams; Copyright (c) 2026 Sam Wolfs; MIT License (see licenses/synaptic-sms-MIT.txt).
from __future__ import annotations

R8 = ("b", "c", "d", "e", "h", "l", "(hl)", "a")
RP = ("bc", "de", "hl", "sp")
RP2 = ("bc", "de", "hl", "af")
CC = ("nz", "z", "nc", "c", "po", "pe", "p", "m")
ALU = ("add a,", "adc a,", "sub", "sbc a,", "and", "xor", "or", "cp")
ROT = ("rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl")
IM = (0, 0, 1, 2, 0, 0, 1, 2)
BLI = (("ldi", "cpi", "ini", "outi"), ("ldd", "cpd", "ind", "outd"),
       ("ldir", "cpir", "inir", "otir"), ("lddr", "cpdr", "indr", "otdr"))


def _signed(byte: int) -> int:
    return byte - 0x100 if byte >= 0x80 else byte


def _disp(byte: int) -> str:
    d = _signed(byte)
    return f"+${d:02x}" if d >= 0 else f"-${-d:02x}"


class _Reader:
    """Bytes of one instruction, read in order; running off the end makes the whole thing `db`."""

    def __init__(self, data: bytes, addr: int):
        self.data, self.addr, self.pos = data, addr, 0

    def u8(self) -> int:
        if self.pos >= len(self.data):
            raise IndexError
        b = self.data[self.pos]
        self.pos += 1
        return b

    def u16(self) -> int:
        lo = self.u8()
        return lo | (self.u8() << 8)


def _reg(i: int, idx: str | None, r: _Reader, use_mem: bool) -> str:
    """Register `i` of the r table. With a DD/FD prefix, (hl) becomes (ix+d) (reading d) and, when no (hl) is
    involved in the instruction, h and l become the index register's halves."""
    if i == 6:
        if idx:
            return f"({idx}{_disp(r.u8())})"
        return "(hl)"
    if idx and not use_mem and i in (4, 5):
        return f"{idx}{'h' if i == 4 else 'l'}"
    return R8[i]


def _decode_cb(r: _Reader, idx: str | None) -> str:
    if idx:  # DDCB d op / FDCB d op: the displacement comes before the opcode
        d = r.u8()
        op = r.u8()
        mem = f"({idx}{_disp(d)})"
        x, y, z = op >> 6, (op >> 3) & 7, op & 7
        copy = f", {R8[z]}" if z != 6 else ""  # undocumented: the result is also copied to a register
        if x == 0:
            return f"{ROT[y]} {mem}{copy}"
        if x == 1:
            return f"bit {y}, {mem}"
        return f"{'res' if x == 2 else 'set'} {y}, {mem}{copy}"
    op = r.u8()
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    if x == 0:
        return f"{ROT[y]} {R8[z]}"
    if x == 1:
        return f"bit {y}, {R8[z]}"
    return f"{'res' if x == 2 else 'set'} {y}, {R8[z]}"


def _decode_ed(r: _Reader) -> str:
    op = r.u8()
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    if x == 1:
        if z == 0:
            return f"in {R8[y]}, (c)" if y != 6 else "in f, (c)"
        if z == 1:
            return f"out (c), {R8[y]}" if y != 6 else "out (c), 0"
        if z == 2:
            return f"{'sbc' if q == 0 else 'adc'} hl, {RP[p]}"
        if z == 3:
            n = r.u16()
            return f"ld (${n:04x}), {RP[p]}" if q == 0 else f"ld {RP[p]}, (${n:04x})"
        if z == 4:
            return "neg"
        if z == 5:
            return "reti" if y == 1 else "retn"
        if z == 6:
            return f"im {IM[y]}"
        return ("ld i, a", "ld r, a", "ld a, i", "ld a, r", "rrd", "rld", "nop", "nop")[y]
    if x == 2 and z <= 3 and y >= 4:
        return BLI[y - 4][z]
    raise ValueError(op)  # an ED opcode the CPU does not define


def _decode_main(r: _Reader, op: int, idx: str | None, addr: int) -> tuple[str, bool]:
    """(text, used_the_index_register) for the unprefixed opcode `op`; `idx` is "ix"/"iy" under a DD/FD prefix."""
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1
    hl = idx or "hl"
    used = False

    def rel() -> str:
        d = _signed(r.u8())
        return f"${(addr + r.pos + d) & 0xFFFF:04x}"

    def rp(i: int) -> str:
        nonlocal used
        if i == 2 and idx:
            used = True
        return hl if i == 2 else RP[i]

    def rp2(i: int) -> str:
        nonlocal used
        if i == 2 and idx:
            used = True
        return hl if i == 2 else RP2[i]

    def reg(i: int, use_mem: bool = False) -> str:
        nonlocal used
        if idx and (i == 6 or (not use_mem and i in (4, 5))):
            used = True
        return _reg(i, idx, r, use_mem)

    if x == 0:
        if z == 0:
            if y == 0:
                return "nop", False
            if y == 1:
                return "ex af, af'", False
            if y == 2:
                return f"djnz {rel()}", False
            if y == 3:
                return f"jr {rel()}", False
            return f"jr {CC[y - 4]}, {rel()}", False
        if z == 1:
            if q == 0:
                name = rp(p)
                return f"ld {name}, ${r.u16():04x}", used
            return f"add {hl}, {rp(p)}", used or bool(idx)
        if z == 2:
            if q == 0:
                if p == 0:
                    return "ld (bc), a", False
                if p == 1:
                    return "ld (de), a", False
                if p == 2:
                    return f"ld (${r.u16():04x}), {hl}", bool(idx)
                return f"ld (${r.u16():04x}), a", False
            if p == 0:
                return "ld a, (bc)", False
            if p == 1:
                return "ld a, (de)", False
            if p == 2:
                return f"ld {hl}, (${r.u16():04x})", bool(idx)
            return f"ld a, (${r.u16():04x})", False
        if z == 3:
            return f"{'inc' if q == 0 else 'dec'} {rp(p)}", used
        if z in (4, 5):
            name = reg(y)
            return f"{'inc' if z == 4 else 'dec'} {name}", used
        if z == 6:
            name = reg(y)
            return f"ld {name}, ${r.u8():02x}", used
        return ("rlca", "rrca", "rla", "rra", "daa", "cpl", "scf", "ccf")[y], False
    if x == 1:
        if y == 6 and z == 6:
            return "halt", False
        # an (hl) operand keeps the other side's h/l as they are; otherwise h/l are the index halves
        mem = y == 6 or z == 6
        a = reg(y, mem)
        b = reg(z, mem)
        return f"ld {a}, {b}", used
    if x == 2:
        name = reg(z)
        return f"{ALU[y]} {name}", used
    if z == 0:
        return f"ret {CC[y]}", False
    if z == 1:
        if q == 0:
            return f"pop {rp2(p)}", used
        return (("ret", False), ("exx", False), (f"jp ({hl})", bool(idx)), (f"ld sp, {hl}", bool(idx)))[p]
    if z == 2:
        return f"jp {CC[y]}, ${r.u16():04x}", False
    if z == 3:
        if y == 0:
            return f"jp ${r.u16():04x}", False
        if y == 2:
            return f"out (${r.u8():02x}), a", False
        if y == 3:
            return f"in a, (${r.u8():02x})", False
        if y == 4:
            return f"ex (sp), {hl}", bool(idx)
        if y == 5:
            return "ex de, hl", False
        if y == 6:
            return "di", False
        return "ei", False
    if z == 4:
        return f"call {CC[y]}, ${r.u16():04x}", False
    if z == 5:
        if q == 0:
            return f"push {rp2(p)}", used
        return f"call ${r.u16():04x}", False
    if z == 6:
        return f"{ALU[y]} ${r.u8():02x}", False
    return f"rst ${y * 8:02x}", False


def decode(data: bytes, addr: int) -> tuple[str, int]:
    """(text, length) for the instruction at the start of `data`, which sits at address `addr`.

    Undefined opcodes, a prefix that changes nothing, and bytes past the end of the buffer decode as one
    `db $xx` so a sweep over the ROM never desynchronises."""
    if not data:
        return "db $00", 1
    first = data[0]
    r = _Reader(data, addr)
    try:
        op = r.u8()
        if op == 0xCB:
            text = _decode_cb(r, None)
        elif op == 0xED:
            try:
                text = _decode_ed(r)
            except ValueError:
                return f"db ${first:02x}, ${data[1]:02x}", 2  # the CPU treats it as a two-byte nop
        elif op in (0xDD, 0xFD):
            idx = "ix" if op == 0xDD else "iy"
            nxt = r.u8()
            if nxt == 0xCB:
                text = _decode_cb(r, idx)
            elif nxt in (0xDD, 0xED, 0xFD):
                return f"db ${first:02x}", 1  # a prefix followed by another prefix: the first one does nothing
            else:
                text, used = _decode_main(r, nxt, idx, addr)
                if not used:
                    return f"db ${first:02x}", 1  # an opcode the index register doesn't touch: the prefix is a nop
        else:
            text, _ = _decode_main(r, op, None, addr)
    except (IndexError, ValueError):
        return f"db ${first:02x}", 1
    return text, r.pos


def length(data: bytes, addr: int = 0) -> int:
    return decode(data, addr)[1]


def text_at(data: bytes, addr: int) -> str:
    return decode(data, addr)[0]


def disassemble(data: bytes, base: int = 0, count: int | None = None, start: int = 0):
    """Yield (address, text, length) walking forward from `start`; stops at the end of the buffer or after `count`
    instructions. A byte that cannot be decoded is emitted as data and advances by one."""
    addr, off = base + start, start
    emitted = 0
    while off < len(data) and (count is None or emitted < count):
        text, ln = decode(data[off:off + 4], addr)
        yield addr, text, ln
        off += max(1, ln)
        addr += max(1, ln)
        emitted += 1


def asm_text(code: bytes, base: int, width: int = 8, data: dict[int, int] | None = None) -> str:
    """The fork's stored asm form: `ADDR  mnemonic operands` per line. `data` ({address: length}): bytes in it that
    aren't instructions (what a routine reads after its call), shown as `db`."""
    if not data:
        return "\n".join(f"{addr:0{width}x}  {text}" for addr, text, _ in disassemble(code, base))
    out, off = [], 0
    while off < len(code):
        addr = base + off
        if (n := data.get(addr)):
            chunk = code[off:off + n]
            out.append(f"{addr:0{width}x}  db " + ", ".join(f"${b:02x}" for b in chunk))
            off += max(1, len(chunk))
            continue
        text, ln = decode(code[off:off + 4], addr)
        out.append(f"{addr:0{width}x}  {text}")
        off += max(1, ln)
    return "\n".join(out)


# ---------------------------------------------------------------- control flow

def flow(text: str) -> tuple[str, int | None]:
    """How an instruction moves control: (kind, target address or None).

    ret/jump end straight-line flow; cret/cjump/call/rst fall through as well; `jp (hl)` and its kin end it
    with no known target."""
    mnemonic, _, ops = text.partition(" ")
    ops = ops.strip()
    if mnemonic in ("ret", "reti", "retn"):
        return ("cret" if ops else "ret"), None
    if mnemonic == "jp":
        if ops.startswith("("):
            return "ret", None  # jp (hl) / (ix) / (iy): leaves, target unknown
        if "," in ops:
            return "cjump", int(ops.rsplit("$", 1)[1], 16)
        return "jump", int(ops.rsplit("$", 1)[1], 16)
    if mnemonic == "jr":
        return ("cjump" if "," in ops else "jump"), int(ops.rsplit("$", 1)[1], 16)
    if mnemonic == "djnz":
        return "cjump", int(ops.rsplit("$", 1)[1], 16)
    if mnemonic == "call":
        return "call", int(ops.rsplit("$", 1)[1], 16)
    if mnemonic == "rst":
        return "call", int(ops.lstrip("$"), 16)
    return "plain", None


def is_return(text: str) -> bool:
    """True for an instruction that ends a function: an unconditional return, or an unconditional jump (a tail
    call or the end of a loop body). A conditional return or jump falls through, so code follows it."""
    return flow(text)[0] in ("ret", "jump")


# ---------------------------------------------------------------- capstone-shaped adapter

#: Instructions that change control flow (what the scoring normaliser asks capstone's CS_GRP_JUMP about).
_JUMPY = frozenset({"jp", "jr", "djnz", "call", "ret", "reti", "retn", "rst", "halt"})


class Insn:
    """The subset of capstone's instruction object the scoring pipeline reads (`id`, `group`, the operand text and
    the encoding), so a Z80 candidate travels the same path as every other architecture."""

    __slots__ = ("address", "size", "bytes", "mnemonic", "op_str", "id")

    def __init__(self, address: int, size: int, raw: bytes, mnemonic: str, op_str: str):
        self.address, self.size, self.bytes = address, size, raw
        self.mnemonic, self.op_str = mnemonic, op_str
        self.id = 0 if mnemonic == "db" else 1  # capstone renders undecodable bytes with id 0

    def group(self, _group) -> bool:
        return self.mnemonic in _JUMPY


class Decoder:
    """`capstone.Cs`-shaped Z80 decoder: `for ins in Decoder().disasm(code, addr)`."""

    version = "z80-1"

    def disasm(self, code: bytes, addr: int = 0):
        for address, text, size in disassemble(code, addr):
            mnemonic, _, op_str = text.partition(" ")
            yield Insn(address, size, code[address - addr:address - addr + size], mnemonic, op_str)
