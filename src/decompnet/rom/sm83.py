"""Sharp LR35902 (SM83) disassembler for Game Boy and Game Boy Color ROMs.

Why this exists: the fork decodes candidate instructions with capstone for every other architecture
(MIPS, ARM/Thumb, PPC, x86), but capstone has no SM83 backend. Game Boy support therefore brings its
own decoder, and it is deliberately complete rather than best-effort: all 256 base opcodes and all 256
CB-prefixed opcodes decode, and the 11 undocumented opcodes come back as data (`db $xx`) exactly as a
hardware-accurate disassembler should render them.

Text style follows the fork's other lanes (lower-case mnemonics, `$` hex, no `a,` noise removed):
`ld a, $12` · `ld hl, $c000` · `jr nz, $0150` · `call $2000` · `ld (hl+), a` · `add sp, $08`.
"""

from __future__ import annotations

# 8-bit registers as they appear in the `ld r, r'` and ALU blocks, indexed by the low three bits.
R8 = ("b", "c", "d", "e", "h", "l", "(hl)", "a")
ALU = ("add", "adc", "sub", "sbc", "and", "xor", "or", "cp")
CB_ROT = ("rlc", "rrc", "rl", "rr", "sla", "sra", "swap", "srl")
RP = ("bc", "de", "hl", "sp")     # ld rp, nn / inc rp / add hl, rp
RP_PUSH = ("bc", "de", "hl", "af")  # push / pop
CC = ("nz", "z", "nc", "c")

# Opcodes the CPU does not implement: a disassembler renders them as one data byte so a sweep over the
# ROM never loses alignment (each is a single byte, so decoding continues correctly afterwards).
UNDEFINED = frozenset({0xD3, 0xDB, 0xDD, 0xE3, 0xE4, 0xEB, 0xEC, 0xED, 0xF4, 0xFC, 0xFD})


def _base_table() -> list[tuple[str, str, int]]:
    """(mnemonic, operand template, length) for every base opcode. `{n8}`, `{n16}`, `{rel}` and `{r8}`
    are filled in at decode time; `{r8}` is the register name for that opcode."""
    ops: list[tuple[str, str, int]] = [("db", "{raw}", 1)] * 256
    ops[0x00] = ("nop", "", 1)
    ops[0x10] = ("stop", "$00", 2)  # the second byte is padding and is always zero

    for i, rp in enumerate(RP):
        ops[0x01 + i * 0x10] = ("ld", f"{rp}, {{n16}}", 3)
        ops[0x03 + i * 0x10] = ("inc", rp, 1)
        ops[0x0B + i * 0x10] = ("dec", rp, 1)

    for i in range(8):  # inc/dec/ld r, n on the 0x04/0x05/0x06 grid
        r = R8[i]
        ops[0x04 + i * 8] = ("inc", r, 1)
        ops[0x05 + i * 8] = ("dec", r, 1)
        ops[0x06 + i * 8] = ("ld", f"{r}, {{n8}}", 2)

    for i, pair in enumerate(("(bc)", "(de)", "(hl+)", "(hl-)")):
        ops[0x02 + i * 0x10] = ("ld", f"{pair}, a", 1)
        ops[0x0A + i * 0x10] = ("ld", f"a, {pair}", 1)

    ops[0x07], ops[0x0F], ops[0x17], ops[0x1F] = ("rlca", "", 1), ("rrca", "", 1), ("rla", "", 1), ("rra", "", 1)
    ops[0x08] = ("ld", "({n16}), sp", 3)
    for i, rp in enumerate(RP):
        ops[0x09 + i * 0x10] = ("add", f"hl, {rp}", 1)
    ops[0x27], ops[0x2F], ops[0x37], ops[0x3F] = ("daa", "", 1), ("cpl", "", 1), ("scf", "", 1), ("ccf", "", 1)

    ops[0x18] = ("jr", "{rel}", 2)
    for i, cc in enumerate(CC):
        ops[0x20 + i * 8] = ("jr", f"{cc}, {{rel}}", 2)

    for op in range(0x40, 0x80):  # ld r, r'
        if op == 0x76:
            ops[op] = ("halt", "", 1)
        else:
            ops[op] = ("ld", f"{R8[(op >> 3) & 7]}, {R8[op & 7]}", 1)
    for op in range(0x80, 0xC0):  # alu r: add/adc name the accumulator, sub/sbc/and/xor/or/cp do not
        op_name = ALU[(op >> 3) & 7]
        ops[op] = (op_name, f"a, {R8[op & 7]}" if op_name in ("add", "adc") else R8[op & 7], 1)

    for i, cc in enumerate(CC):
        ops[0xC0 + i * 8] = ("ret", cc, 1)
        ops[0xC2 + i * 8] = ("jp", f"{cc}, {{n16}}", 3)
        ops[0xC4 + i * 8] = ("call", f"{cc}, {{n16}}", 3)
    for i in range(8):
        ops[0xC7 + i * 8] = ("rst", f"${i * 8:02x}", 1)
    ops[0xC1 + 0], ops[0xC1 + 0x10], ops[0xC1 + 0x20], ops[0xC1 + 0x30] = (
        ("pop", rp, 1) for rp in RP_PUSH)
    ops[0xC5], ops[0xD5], ops[0xE5], ops[0xF5] = (("push", rp, 1) for rp in RP_PUSH)
    ops[0xC9], ops[0xD9] = ("ret", "", 1), ("reti", "", 1)
    ops[0xC3] = ("jp", "{n16}", 3)
    ops[0xCD] = ("call", "{n16}", 3)

    for i, name in enumerate(("add", "adc", "sub", "sbc", "and", "xor", "or", "cp")):
        ops[0xC6 + i * 8] = (name, "a, {n8}" if name not in ("sub", "and", "xor", "or", "cp") else "{n8}", 2)

    ops[0xE0], ops[0xF0] = ("ldh", "($ff00+{n8}), a", 2), ("ldh", "a, ($ff00+{n8})", 2)
    ops[0xE2], ops[0xF2] = ("ld", "($ff00+c), a", 1), ("ld", "a, ($ff00+c)", 1)
    ops[0xE8], ops[0xF8] = ("add", "sp, {n8}", 2), ("ld", "hl, sp+{n8}", 2)
    ops[0xE9], ops[0xF9] = ("jp", "hl", 1), ("ld", "sp, hl", 1)
    ops[0xEA], ops[0xFA] = ("ld", "({n16}), a", 3), ("ld", "a, ({n16})", 3)
    ops[0xF3], ops[0xFB] = ("di", "", 1), ("ei", "", 1)
    for op in UNDEFINED:
        ops[op] = ("db", "{raw}", 1)
    return ops


_BASE = _base_table()


def _cb_text(byte: int) -> tuple[str, int]:
    """CB-prefixed opcode: rotate/shift, bit, res and set."""
    reg = R8[byte & 7]
    block, sub = byte >> 6, (byte >> 3) & 7
    if block == 0:
        return f"{CB_ROT[sub]} {reg}", 2
    if block == 1:
        return f"bit {sub}, {reg}", 2
    return f"{'res' if block == 2 else 'set'} {sub}, {reg}", 2


def _signed(byte: int) -> int:
    return byte - 0x100 if byte >= 0x80 else byte


def decode(data: bytes, addr: int) -> tuple[str, int]:
    """(text, length) for the instruction at `addr` inside `data` (which is addressed from `addr`).

    `text` has no address prefix; `length` is 1-3 bytes. Undefined opcodes and bytes past the end of
    the buffer decode as `db $xx` (length 1) so a sweep never desynchronises."""
    def at(off: int) -> int | None:
        return data[off] if 0 <= off < len(data) else None

    op = at(0)
    if op is None:
        return "db $00", 1
    if op == 0xCB:
        nxt = at(1)
        if nxt is None:
            return "db $cb", 1
        text, length = _cb_text(nxt)
        return text, length

    mnemonic, fmt, length = _BASE[op]
    if fmt == "{raw}":
        return f"db ${op:02x}", 1
    if "{n8}" in fmt or "{rel}" in fmt:
        n8 = at(1)
        if n8 is None:
            return f"db ${op:02x}", 1
        if "{rel}" in fmt:  # a relative jump: the operand is an absolute target, computed from here
            target = (addr + 2 + _signed(n8)) & 0xFFFF
            return f"{mnemonic} " + fmt.replace("{rel}", f"${target:04x}"), length
        if mnemonic == "add" and fmt.startswith("sp,"):
            return f"add sp, ${_signed(n8) & 0xFF:02x}", length
        if mnemonic == "ld" and fmt.startswith("hl, sp+"):
            return f"ld hl, sp+${_signed(n8) & 0xFF:02x}", length
        return f"{mnemonic} " + fmt.replace("{n8}", f"${n8:02x}"), length
    if "{n16}" in fmt:
        lo, hi = at(1), at(2)
        if lo is None or hi is None:
            return f"db ${op:02x}", 1
        n16 = lo | (hi << 8)
        return f"{mnemonic} " + fmt.replace("{n16}", f"${n16:04x}"), length
    return (f"{mnemonic} {fmt}".strip(), length)


def length(data: bytes, addr: int = 0) -> int:
    return decode(data, addr)[1]


def text_at(data: bytes, addr: int) -> str:
    return decode(data, addr)[0]


def disassemble(data: bytes, base: int = 0, count: int | None = None, start: int = 0):
    """Yield (address, text, length) walking forward from `start`; stops at the end of the buffer or
    after `count` instructions. A byte that cannot be decoded is emitted as data and advances by one."""
    addr, off = base + start, start
    emitted = 0
    while off < len(data) and (count is None or emitted < count):
        text, ln = decode(data[off:], addr)
        yield addr, text, ln
        off += max(1, ln)
        addr += max(1, ln)
        emitted += 1


def asm_text(code: bytes, base: int, width: int = 8) -> str:
    """The fork's stored asm form: `ADDR  mnemonic operands` per line (no address for continuation bytes)."""
    lines = []
    for addr, text, ln in disassemble(code, base):
        lines.append(f"{addr:0{width}x}  {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------- discovery helpers

def call_target(code: bytes, addr: int) -> int | None:
    """The absolute target of a `call nn` at `addr`, else None (used to find function starts)."""
    if addr < 0 or addr >= len(code) or code[addr] != 0xCD:
        return None
    if addr + 2 >= len(code):
        return None
    return code[addr + 1] | (code[addr + 2] << 8)


def jump_target(code: bytes, addr: int) -> int | None:
    """The absolute target of an unconditional `jp nn` at `addr`, else None."""
    if addr < 0 or addr >= len(code) or code[addr] != 0xC3:
        return None
    if addr + 2 >= len(code):
        return None
    return code[addr + 1] | (code[addr + 2] << 8)


def is_return(text: str) -> bool:
    return text in ("ret", "reti") or text.startswith(("ret ", "jp hl", "jr "))


def flow(text: str) -> tuple[str, int | None]:
    """How an instruction moves control: ret | jump (both end straight-line flow) | cret | cjump | call | plain, and
    its target's CPU address when it has one. `jp hl` leaves with no known target; `rst n` calls `n`."""
    mnemonic, _, ops = text.partition(" ")
    ops = ops.strip()
    if mnemonic in ("ret", "reti"):
        return ("cret" if ops else "ret"), None
    if mnemonic == "jp" and ops == "hl":
        return "ret", None
    target = int(ops.rsplit("$", 1)[1], 16) if "$" in ops and mnemonic in ("jp", "jr", "call") else None
    if mnemonic in ("jp", "jr"):
        return ("cjump" if "," in ops else "jump"), target
    if mnemonic == "call":
        return "call", target
    if mnemonic == "rst":
        return "call", int(ops.lstrip("$"), 16) if ops.lstrip("$") else None
    return "plain", None


def terminates(code: bytes, addr: int) -> bool:
    """True when the instruction at `addr` ends a function's straight-line flow."""
    text, _ = decode(code[addr:], addr)
    return is_return(text)


# ---------------------------------------------------------------- capstone-shaped adapter

#: Instructions that change control flow. The scoring normaliser asks capstone for CS_GRP_JUMP; the
#: adapter answers for that group (and any other) from this set, since the caller only asks about jumps.
_JUMPY = frozenset({"jp", "jr", "call", "ret", "reti", "rst", "halt"})


class Insn:
    """The subset of capstone's instruction object the scoring pipeline reads (`id`, `group`, the
    operand text and the encoding), so an SM83 candidate travels the same path as every other arch."""

    __slots__ = ("address", "size", "bytes", "mnemonic", "op_str", "id")

    def __init__(self, address: int, size: int, raw: bytes, mnemonic: str, op_str: str):
        self.address, self.size, self.bytes = address, size, raw
        self.mnemonic, self.op_str = mnemonic, op_str
        # capstone gives a real instruction a non-zero id and renders undecodable bytes with id 0
        self.id = 0 if mnemonic == "db" else 1

    def group(self, _group) -> bool:
        return self.mnemonic in _JUMPY


class Decoder:
    """`capstone.Cs`-shaped SM83 decoder: `for ins in Decoder().disasm(code, addr)`.

    Capstone has no SM83 back end, so this is what lets Game Boy candidates go through the same
    preparation and diff path as every other architecture."""

    version = "sm83-1"

    def disasm(self, code: bytes, addr: int = 0):
        for address, text, size in disassemble(code, addr):
            mnemonic, _, op_str = text.partition(" ")
            yield Insn(address, size, code[address - addr:address - addr + size], mnemonic, op_str)
