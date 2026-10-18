"""Following a Game Boy game's code with the banks it maps, to tell its code from its data.

mgbdis decodes every byte of a bank as code unless a symbol file says otherwise, so graphics, tables and text come out
as instructions, the `call`-like bytes in them as labels, and the split hands those out as functions (rom/gb_mgbdis).
So the code is followed first, from the reset and interrupt vectors and the cartridge's entry point, instruction by
instruction, keeping track of the bank in the 0x4000-0x7FFF window:

- `ld a,5 / ld ($2000),a` (and through HL: `ld hl,$2100 / ld (hl),a`) maps a bank. The mapper reads only as many bits
  as the ROM needs, and bank 0 asked for there is bank 1 on MBC1, MBC2 and MBC3;
- code running in a switchable bank reaches its own bank in the window; code in bank 0 the bank last mapped (not
  known: not followed);
- a function that writes the A it was called with to the mapper maps the bank its callers pass in A;
- a function that never returns, or that takes its return address (data follows its call: `rst $28 / dw a, b`), ends
  the straight-line code at the call;
- a jump table handed to a dispatcher in HL (`ld hl,table / rst $28`) names its handlers.

Rounds repeat that with what the last one learned until nothing changes (the Master System lane's tracer,
rom/sms_trace.py, works the same way). What it reaches is code for certain; `regions` then sorts what it didn't reach:
padding, data by its look (bytes that are no instruction, `rst $38`, a register loaded with itself), and what reads
like code, which mgbdis still decodes. Fed to mgbdis as a symbol file (`sym_text`), the data is shown as data and its
bytes no longer start functions; the rebuild check stays the proof that nothing was lost.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from . import sm83

BANK = 0x4000
VECTORS = tuple(range(0x00, 0x40, 0x08)) + (0x40, 0x48, 0x50, 0x58, 0x60)
ENTRY = 0x100
HEADER = (0x104, 0x150)  # logo, title, codes, checksums: mgbdis has them as data already
NOPS = 16  # this many zero bytes in a row are padding, not code
SIGN = 2  # code-like bytes that call this many functions the code reached are code (one can be bytes of data by chance)
SWITCH = 0x2100  # a bank register on every mapper (MBC2 wants bit 8 set)
ARG = ("arg", "a")  # A as the function was entered with it: a function that maps it is a bank switcher

_POP_RET = re.compile(r"^pop (hl|de|bc)$")
_SP_HL = re.compile(r"^ld hl, sp\+\$([0-9a-f]{2})$")
_THROUGH = {"hl": re.compile(r"\(hl[+-]?\)"), "de": re.compile(r"\(de\)"), "bc": re.compile(r"\(bc\)")}
SELF_MOVE = re.compile(r"^ld ([abcdehl]), \1$")  # ld b, b: a register to itself (what 0x40 0x49 ... bytes decode to)
R8 = frozenset("abcdehl")
PAIRS = {"bc": ("b", "c"), "de": ("d", "e"), "hl": ("h", "l")}
_N8 = re.compile(r"^\$([0-9a-f]{2})$")
_N16 = re.compile(r"^\$([0-9a-f]{4})$")
_MEM = re.compile(r"^\(\$([0-9a-f]{4})\)$")
_HMEM = re.compile(r"^\(\$ff00\+\$([0-9a-f]{2})\)$")
ROM_END, RAM = 0x8000, ((0xA000, 0xE000), (0xFF80, 0xFFFF))
_LOAD_A = re.compile(r"^(?:ld a, \(\$([0-9a-f]{4})\)|ldh a, \(\$ff00\+\$([0-9a-f]{2})\))$")


def _addr(m) -> int:
    return int(m.group(1), 16) if m.group(1) else 0xFF00 + int(m.group(2), 16)


def _ram(addr: int) -> bool:
    return any(lo <= addr < hi for lo, hi in RAM)


def mapper(data: bytes) -> str:
    """The cartridge's bank switching, from its header: none | mbc1 | mbc2 | mbc3 | mbc5 (others switch like mbc5)."""
    cart = data[0x147] if len(data) > 0x147 else 0
    if cart in (0x00, 0x08, 0x09) and len(data) <= 2 * BANK:
        return "none"
    if cart in (0x01, 0x02, 0x03):
        return "mbc1"
    if cart in (0x05, 0x06):
        return "mbc2"
    if 0x0F <= cart <= 0x13:
        return "mbc3"
    return "mbc5"


def bank_mask(size: int) -> int:
    banks = 1
    while banks * BANK < size:
        banks <<= 1
    return banks - 1


def cpu_of(flat: int) -> int:
    return flat if flat < BANK else BANK + flat % BANK


@dataclass
class State:
    """What's known as the code runs: the bank in the 0x4000-0x7FFF window (an int, or None: not known), the 8-bit
    registers (an int, ARG for A as the function was entered with it, or None), the ROM address HL was last loaded with
    (a table it may index), and bytes stored to RAM (a function pointer kept there)."""

    bank: object
    r: dict = field(default_factory=dict)
    hl_base: int | None = None
    mem: dict = field(default_factory=dict)

    def copy(self) -> State:
        return State(self.bank, dict(self.r), self.hl_base, dict(self.mem))

    def pair(self, name: str) -> int | None:
        hi, lo = (self.r.get(x) for x in PAIRS[name])
        return hi << 8 | lo if isinstance(hi, int) and isinstance(lo, int) else None

    def set_pair(self, name: str, value: int | None) -> None:
        hi, lo = PAIRS[name]
        self.r[hi], self.r[lo] = (None, None) if value is None else ((value >> 8) & 0xFF, value & 0xFF)

    def value(self, operand: str):
        """An 8-bit operand's value: a register's, or an immediate."""
        if operand in R8:
            return self.r.get(operand)
        m = _N8.match(operand)
        return int(m.group(1), 16) if m else None


_ALU = {"and": lambda a, b: a & b, "or": lambda a, b: a | b, "xor": lambda a, b: a ^ b,
        "add": lambda a, b: (a + b) & 0xFF, "sub": lambda a, b: (a - b) & 0xFF}


def step(st: State, text: str) -> None:
    """What an instruction that isn't a call does to the registers (constants carried through, the rest forgotten)."""
    op, _, rest = text.partition(" ")
    args = [x.strip() for x in rest.split(",")] if rest else []
    r = st.r
    if op == "ld" and len(args) == 2:
        dst, src = args
        if dst in R8:
            if src in R8:
                r[dst] = r.get(src)
            else:
                r[dst] = st.value(src) if _N8.match(src) else None
        elif dst in PAIRS and (m := _N16.match(src)):
            st.set_pair(dst, int(m.group(1), 16))
            if dst == "hl":
                st.hl_base = int(m.group(1), 16)
        elif dst == "hl":  # ld hl, sp+n
            st.set_pair("hl", None)
            st.hl_base = None
        for x in (dst, src):  # (hl+) and (hl-) move HL
            if x in ("(hl+)", "(hl-)") and (hl := st.pair("hl")) is not None:
                st.set_pair("hl", (hl + (1 if x == "(hl+)" else -1)) & 0xFFFF)
            elif x in ("(hl+)", "(hl-)"):
                st.set_pair("hl", None)
    elif op == "ldh" and args and args[0] == "a":
        r["a"] = None
    elif op in ("inc", "dec") and args:
        x, d = args[0], (1 if op == "inc" else -1)
        if x in R8:
            r[x] = (r[x] + d) & 0xFF if isinstance(r.get(x), int) else None
        elif x in PAIRS:
            v = st.pair(x)
            st.set_pair(x, None if v is None else (v + d) & 0xFFFF)
    elif op == "xor" and args == ["a"]:
        r["a"] = 0
    elif op in ("and", "or", "xor", "sub") and args:
        a, b = r.get("a"), st.value(args[-1])
        r["a"] = _ALU[op](a, b) if isinstance(a, int) and isinstance(b, int) else None
    elif op in ("add", "adc") and args and args[0] == "a":
        a, b = r.get("a"), st.value(args[-1])
        r["a"] = _ALU["add"](a, b) if op == "add" and isinstance(a, int) and isinstance(b, int) else None
    elif op == "add" and args and args[0] == "hl":
        v, w = st.pair("hl"), st.pair(args[1]) if args[1] in PAIRS else None
        st.set_pair("hl", None if v is None or w is None else (v + w) & 0xFFFF)
    elif op in ("sbc", "cpl", "daa", "rla", "rra", "rlca", "rrca"):
        r["a"] = None
    elif op in ("rlc", "rrc", "rl", "rr", "sla", "sra", "srl", "swap") and args and args[0] in R8:
        r[args[0]] = None
    elif op in ("res", "set") and len(args) == 2 and args[1] in R8:
        v = r.get(args[1])
        bit = 1 << int(args[0])
        r[args[1]] = (v & ~bit if op == "res" else v | bit) & 0xFF if isinstance(v, int) else None
    elif op == "pop" and args:
        if args[0] == "af":
            r["a"] = None
        else:
            st.set_pair(args[0], None)
            if args[0] == "hl":
                st.hl_base = None


def store(st: State, text: str) -> tuple[int, object] | None:
    """(address, value) when the instruction writes a byte to a known address (the value an int, ARG or None)."""
    op, _, rest = text.partition(" ")
    args = [x.strip() for x in rest.split(",")] if rest else []
    if len(args) != 2 or op not in ("ld", "ldh"):
        return None
    dst, src = args
    if m := _MEM.match(dst):
        return int(m.group(1), 16), st.value(src)
    if m := _HMEM.match(dst):
        return 0xFF00 + int(m.group(1), 16), st.value(src)
    for name, pat in _THROUGH.items():
        if pat.fullmatch(dst) and (addr := st.pair(name)) is not None:
            return addr, st.value(src)
    return None


@dataclass
class Found:
    starts: dict[int, tuple[int, str]] = field(default_factory=dict)  # flat -> (cpu, how it was found)
    entered: dict[int, set] = field(default_factory=dict)  # flat -> the window banks it was entered with
    returns: set[int] = field(default_factory=set)  # functions a path of which returns (through tail jumps too)
    tails: dict[int, set] = field(default_factory=dict)  # function -> where it jumps out to (None: RAM, a bank not known)
    takes_return: set[int] = field(default_factory=set)  # functions that pop their return address first
    switchers: set[int] = field(default_factory=set)  # functions that map the A they are given
    dispatchers: set[int] = field(default_factory=set)  # functions that jump through HL to an address they work out
    with_hl: list[tuple[int, int, object, int]] = field(default_factory=list)  # (from flat, HL, window bank, to flat)
    jumps: list[tuple[int, int, int]] = field(default_factory=list)  # (function, to flat, to cpu)
    anchors: set[int] = field(default_factory=set)  # starts reached by something other than a jump
    mbc_writes: int = 0
    code: dict[int, int] = field(default_factory=dict)  # flat -> length: every instruction reached
    tables: dict[int, int] = field(default_factory=dict)  # flat -> how many words: tables of handler addresses
    # where a table of handlers may be: (from flat, table address, window bank, an inline one: right after a call)
    indexed: list[tuple[int, int, object, bool]] = field(default_factory=list)
    pointers: list[tuple[int, int, object]] = field(default_factory=list)  # (stored by, a ROM address, window bank)
    setters: dict[int, set] = field(default_factory=dict)  # function -> the pairs it stores as a word, as it's given them
    bank_arg: dict[int, str] = field(default_factory=dict)  # function -> the register whose value it maps (a: switchers)
    calls_arg: dict[int, str] = field(default_factory=dict)  # function -> the pair it's given that it jumps through
    inline: dict[int, tuple[int, str]] = field(default_factory=dict)  # flat -> (size, what): operands after a call
    edges: dict[int, int] = field(default_factory=dict)  # a call or jump from bank 0 into the window -> where it went


@dataclass(frozen=True)
class Inline:
    """A routine that reads bytes after its call through its return address and returns past them: how many, which of
    them is the bank it maps (None: none), and where the address it then jumps to is among them (its low byte)."""

    size: int
    bank: int | None = None
    addr: int | None = None

    def why(self) -> str:
        if self.addr is not None and self.bank is not None:
            return f"the bank and address of a far call ({self.size} bytes after the call)"
        return f"{self.size} byte{'s' if self.size != 1 else ''} the routine called reads after its call"


class Tracer:
    def __init__(self, data: bytes, paged: bool | None = None, hints: dict | None = None):
        """`paged`: whether the game maps banks at all (a first pass learns it: `mbc_writes`). Code entered from
        somewhere nothing was followed from (an interrupt handler) can't know what the window holds when it does;
        when it doesn't, the window keeps bank 1. `hints`: what a person or an agent established (`entries`
        {flat: cpu}, `noreturn` {flat}, `data` [(flat, size)], `tables` [(flat, words)], `inline` {routine: bytes},
        `coverage`: what an emulator saw the game run and read, rom/coverage.py)."""
        self.data, self.size = data, len(data)
        self.kind = mapper(data)
        self.mask = bank_mask(self.size)
        self.unknown = None if paged else 1
        self.hints = hints or {}
        cov = self.hints.get("coverage")
        self.no_code = sorted([(f, f + n) for f, n in self.hints.get("data", [])]
                              + ([(f, f + n) for f, n in cov.runs("read")] if cov is not None else []))
        self.found = Found()
        self.known: set[int] = set()
        self.prev_starts: set[int] = set()
        self._dec: dict[int, tuple[str, int]] = {}
        self._shapes: dict[int, Inline | None] = {}
        self._clobbers: dict[int, frozenset] = {}
        for flat, n in self.hints.get("inline", {}).items():  # what a person or an agent established
            self._shapes[flat] = Inline(int(n))

    # -- bytes

    def decode(self, flat: int) -> tuple[str, int]:
        """The instruction at `flat` (cached: on the Game Boy a byte's CPU address follows from where it is). One that
        would run past the end of its bank is a data byte, as mgbdis has it."""
        hit = self._dec.get(flat)
        if hit is None:
            end = min(self.size, (flat // BANK + 1) * BANK)
            text, ln = sm83.decode(self.data[flat:min(end, flat + 3)], cpu_of(flat))
            if flat + ln > end:
                text, ln = f"db ${self.data[flat]:02x}", 1
            hit = self._dec[flat] = (text, ln)
        return hit

    def _hinted_data(self, flat: int) -> bool:
        k = bisect.bisect_right(self.no_code, (flat, 1 << 30)) - 1
        return k >= 0 and self.no_code[k][0] <= flat < self.no_code[k][1]

    def can_start(self, flat: int) -> bool:
        if not 0 <= flat < self.size or self.data[flat:flat + 2] in (b"\xff\xff",) or self._hinted_data(flat):
            return False
        text = self.decode(flat)[0]
        return not text.startswith("db ") and text != "rst $38"

    _ODD_START = re.compile(r"^(?:nop|halt|stop|rst \$38|db |ld \(bc\), a|ld \(de\), a|daa|ccf|scf|di$|ei$)")

    def plausible(self, flat: int, limit: int = 64) -> bool:
        """For an address that's only a guess (a word in a table): code that reads like a function's start, and a
        return or a jump within `limit` instructions in its bank."""
        if not self.can_start(flat) or self.data[flat:flat + 2] == b"\x00\x00":
            return False
        for k in range(limit):
            text, ln = self.decode(flat)
            if text.startswith("db ") or text == "rst $38" or SELF_MOVE.match(text) or (k == 0 and self._ODD_START.match(text)):
                return False
            if sm83.flow(text)[0] in ("ret", "jump"):
                return True
            flat += ln
            if flat % BANK == 0 or flat >= self.size:
                return False
        return False

    def resolve(self, target: int, here: int, bank) -> int | None:
        """The ROM offset a call or jump to CPU address `target` reaches from code at `here` while the window holds
        `bank` (None: not known). None for RAM, registers, an unknown bank, or past the ROM's end."""
        if target < BANK:
            flat = target
        elif target < 2 * BANK:
            if here >= BANK:
                b = here // BANK  # code in the window reaches its own bank there
            elif self.kind == "none":
                b = 1
            else:
                b = bank
            if not isinstance(b, int):
                return None
            flat = b * BANK + target - BANK
        else:
            return None
        return flat if flat < self.size else None

    def bank_register(self, reg: int) -> bool:
        """Whether a write to `reg` picks the ROM bank in the window."""
        if self.kind == "none":
            return False
        if self.kind == "mbc2":
            return reg < 0x4000 and bool(reg & 0x100)
        return 0x2000 <= reg < 0x4000

    def _bank_value(self, reg: int, val, bank):
        """The window's bank after writing `val` to the bank register `reg` (ARG: the A the function was given)."""
        if val == ARG:
            return ARG
        if not isinstance(val, int):
            return None
        if self.kind == "mbc5":
            if reg >= 0x3000:  # bit 8 of the bank
                return ((bank & 0xFF) | (val & 1) << 8) & self.mask if isinstance(bank, int) else None
            return ((bank & 0x100 if isinstance(bank, int) else 0) | val) & self.mask
        v = val & {"mbc1": 0x1F, "mbc2": 0x0F, "mbc3": 0x7F}[self.kind]
        return (v or 1) & self.mask or 1

    # -- the rounds

    def follow(self, seeds: dict[int, tuple[int, str]], rounds: int = 8) -> Found:
        more: dict[int, tuple[int, str]] = {}
        before = None
        for _ in range(rounds):
            todo = {**more, **seeds}
            self.known = set(todo)
            self.prev_starts = set(self.found.starts)
            self._round(todo)
            self._returning()
            more = {**self._far_jumps(), **self._tables(), **self._pointers()}
            now = (frozenset(self.found.starts.items()), frozenset(self.found.switchers), frozenset(self.found.returns),
                   frozenset(self.found.takes_return), frozenset(more))
            if now == before:
                break
            before = now
        self._orphans()
        return self.found

    def _orphans(self) -> None:
        """Reached code no function covers starts one: a stretch the code jumps to past data (`jp $0150` over the
        cartridge header) belongs to no function that starts before it."""
        f = self.found
        end = None
        for flat in sorted(f.code):
            if flat != end and flat not in f.starts:  # a run of code that doesn't follow on from the one before
                f.starts[flat] = (cpu_of(flat), "jump")
            end = flat + f.code[flat]

    def _round(self, seeds: dict[int, tuple[int, str]]) -> None:
        f = self.found
        prev, f.entered, f.starts, f.with_hl, f.jumps, f.anchors = f.entered, {}, {}, [], [], set()
        f.code, f.tables, f.mbc_writes, f.indexed, f.pointers, f.tails, f.inline, f.edges = ({}, {}, 0, [], [], {}, {},
                                                                                            {})
        queue = [(flat, cpu, why, 1 if flat == ENTRY else None) for flat, (cpu, why) in sorted(seeds.items())]
        for flat, _cpu, _why, m in queue:
            if m is not None:
                f.entered.setdefault(flat, set()).add(m)
        seen: set[int] = set()
        while queue:
            flat, cpu, why, mapping = queue.pop()
            if why != "jump" and self.can_start(flat):
                f.anchors.add(flat)
            if flat in seen or not self.can_start(flat):
                continue
            seen.add(flat)
            f.starts.setdefault(flat, (cpu, why))
            if flat >= BANK:
                bank = flat // BANK  # the window holds the code running in it
            else:
                entries = prev.get(flat, set()) | ({mapping} if mapping is not None else set())
                banks = {b for b in entries if isinstance(b, int)}
                bank = banks.pop() if len(banks) == 1 and len(entries) == 1 else (self.unknown if not entries else None)
            for cflat, ccpu, m, how in self._trace(flat, State(bank, {x: ("arg", x) for x in R8})):
                f.entered.setdefault(cflat, set()).add(m)
                queue.append((cflat, ccpu, how, m))

    def through(self, flat: int, bank) -> int:
        """Where a call to `flat` really goes: past functions that are only a `jp` to another (an `rst` vector that
        jumps to the routine, a trampoline)."""
        for _ in range(4):
            text, _ln = self.decode(flat)
            kind, target = sm83.flow(text)
            if kind != "jump" or target is None or text.startswith("jr ") or (to := self.resolve(target, flat, bank)) is None:
                break
            flat = to
        return flat

    _READ = re.compile(r"^ld ([abcdehl]), \((hl[+-]?|de|bc)\)$")

    def inline_shape(self, flat: int) -> Inline | None:
        """What a routine that takes its return address reads after its call, from its first instructions: the return
        address popped into a pair, bytes read through it (`ld a,(hl+)`, `ld e,(hl)` / `inc hl`, also through a copy:
        `ld d,h / ld e,l`), then the moved address pushed back (`push hl / ret`) or jumped to. Which byte it maps as a
        bank, and which word it calls or jumps through, come along. None: not such a routine, or not one that can be
        read."""
        if flat in self._shapes:
            return self._shapes[flat]
        self._shapes[flat] = None  # (while it's worked out: a loop back into it isn't one)
        r: dict[str, object] = {}
        ptr: dict[str, int] = {}  # pair -> the offset after the call it points at
        half: dict[str, tuple[str, int, int]] = {}  # register -> (pointer pair, 0 high / 1 low, offset) it was copied from
        resume = bank = addr = back = None
        pos, popped, pushes = flat, False, 0
        stack_hl = None  # where HL points on the stack, from the return address's low byte (`ld hl, sp+$00`)

        def written(reg: str) -> None:
            for pair in [p for p in ptr if reg in PAIRS[p]]:
                ptr.pop(pair)
            half.pop(reg, None)

        for _ in range(48):
            if not 0 <= pos < self.size:
                break
            text, ln = self.decode(pos)
            if text.startswith("db "):
                break
            op, _, rest = text.partition(" ")
            args = [x.strip() for x in rest.split(",")] if rest else []
            if (m := _SP_HL.match(text)):
                stack_hl = int(m.group(1), 16) - 2 * pushes
                pos += ln
                continue
            if text in ("inc hl", "dec hl") and stack_hl is not None and "hl" not in ptr:
                stack_hl += 1 if text == "inc hl" else -1
                pos += ln
                continue
            if not popped:
                if (m := _POP_RET.match(text)) and pushes == 0:
                    ptr[m.group(1)], popped = 0, True
                elif op == "push":
                    pushes += 1
                elif op == "pop":
                    pushes = max(0, pushes - 1)
                elif (m := re.match(r"^ld ([bcde]), \(hl\)$", text)) and stack_hl in (0, 1):
                    half[m.group(1)] = ("ret", 1 - stack_hl, 0)  # the return address read off the stack
                    for pair, (hi, lo) in PAIRS.items():
                        if half.get(hi) == ("ret", 0, 0) and half.get(lo) == ("ret", 1, 0):
                            ptr[pair], popped = 0, True
                elif op in ("call", "rst", "ret", "reti", "jp", "jr") or text == "ld sp, hl":
                    break  # (what it pops or reads then isn't its return address)
                elif op == "ld" and len(args) == 2 and args[0] in R8:
                    r[args[0]] = r.get(args[1]) if args[1] in R8 else None
                pos += ln
                continue
            if (m := re.match(r"^ld \(hl\), ([de])$", text)) and stack_hl in (0, 1) and "de" in ptr:
                # the moved address written back over the return address (either byte first)
                back = dict(back or {}, **{"e" if stack_hl == 0 else "d": ptr["de"]}) if m.group(1) == "ed"[stack_hl] else back
                if back and back.get("e") is not None and back.get("e") == back.get("d"):
                    resume = back["e"]
                pos += ln
                continue
            if (m := self._READ.match(text)) and (pair := m.group(2).rstrip("+-")) in ptr:
                off = ptr[pair]
                written(m.group(1))
                r[m.group(1)] = ("in", off)
                if pair in ptr:
                    ptr[pair] = off + (1 if m.group(2).endswith("+") else -1 if m.group(2).endswith("-") else 0)
            elif op in ("inc", "dec") and args and args[0] in ptr:
                ptr[args[0]] += 1 if op == "inc" else -1
            elif op == "ld" and len(args) == 2 and args[0] in R8 and args[1] in R8:
                dst, src = args
                value = r.get(src)
                copied = next(((p, PAIRS[p].index(src), o) for p, o in ptr.items() if src in PAIRS[p]), half.get(src))
                written(dst)
                r[dst] = value
                if copied is not None:
                    half[dst] = copied
                for pair, (hi, lo) in PAIRS.items():  # both bytes of a pointer copied: that pair is the pointer too
                    a, b = half.get(hi), half.get(lo)
                    if a and b and a[0] == b[0] and (a[1], b[1]) == (0, 1) and a[2] == b[2] and pair not in ptr:
                        ptr[pair] = a[2]
            elif op == "push" and args and args[0] in ptr:
                resume = ptr[args[0]]
            elif (w := store(State(None, dict(r)), text)) is not None and w[0] < ROM_END and self.bank_register(w[0]):
                if isinstance(w[1], tuple) and w[1][0] == "in":
                    bank = w[1][1]
            elif op in ("call", "rst"):
                target = sm83.flow(text)[1]
                to = self.resolve(target, pos, None) if target is not None else None
                if to is None:
                    break
                to = self.through(to, None)
                reg = self.found.bank_arg.get(to)
                if reg and isinstance(r.get(reg), tuple) and r[reg][0] == "in":
                    bank = r[reg][1]
                if self.decode(to)[0] == "jp hl" and (w := self._word(r, "hl")) is not None:
                    addr = w
                for reg in self.clobbers(to):
                    written(reg)
                    r.pop(reg, None)
            elif text == "jp hl":
                if "hl" in ptr and resume is None:
                    resume = ptr["hl"]
                elif (w := self._word(r, "hl")) is not None:
                    addr = w
                break
            elif op in ("ret", "reti") and not args:
                break
            elif op in ("jp", "jr") and "," not in rest:
                target = sm83.flow(text)[1]
                to = self.resolve(target, pos, None) if target is not None else None
                if to is None or to // BANK != flat // BANK:
                    break
                pos = to
                continue
            else:
                before = dict(r)
                st = State(None, r)
                step(st, text)
                r = st.r
                for reg in R8:
                    if r.get(reg) != before.get(reg) or (op in ("pop", "add") and reg in "hlbcde" and args and
                                                          args[0] in PAIRS and reg in PAIRS[args[0]]):
                        written(reg)
            pos += ln
        shape = Inline(resume, bank, addr) if isinstance(resume, int) and 0 < resume <= 8 else None
        self._shapes[flat] = shape
        return shape

    @staticmethod
    def _word(r: dict, pair: str) -> int | None:
        """The offset of the inline word a pair holds (its low byte at the offset, its high byte after), or None."""
        hi, lo = (r.get(x) for x in PAIRS[pair])
        if isinstance(lo, tuple) and isinstance(hi, tuple) and lo[0] == hi[0] == "in" and hi[1] == lo[1] + 1:
            return lo[1]
        return None

    _ALL = frozenset(R8)

    def clobbers(self, flat: int) -> frozenset:
        """The registers a call to `flat` may change: what its straight-line code writes up to its return, for a
        short routine with no calls of its own; every register otherwise."""
        if flat in self._clobbers:
            return self._clobbers[flat]
        self._clobbers[flat] = self._ALL
        out: set[str] = set()
        pos = flat
        for _ in range(24):
            if not 0 <= pos < self.size:
                return self._ALL
            text, ln = self.decode(pos)
            op, _, rest = text.partition(" ")
            args = [x.strip() for x in rest.split(",")] if rest else []
            if op in ("call", "rst", "reti") or text.startswith("db ") or text in ("jp hl", "ld sp, hl"):
                return self._ALL
            if op == "ret" and not args:
                self._clobbers[flat] = frozenset(out)
                return self._clobbers[flat]
            if op in ("jp", "jr") and "," not in rest:
                to = self.resolve(sm83.flow(text)[1], pos, None)
                if to is None:
                    return self._ALL
                pos = to
                continue
            probe = State(None, {x: ("p", x) for x in R8})
            step(probe, text)
            out |= {x for x in R8 if probe.r.get(x) != ("p", x)}
            pos += ln
        return self._ALL

    def _returning(self) -> None:
        """A function returns when a path of it does: a return, a jump to code in RAM or in a bank that isn't known, a
        dispatch through HL (its handler returns for it), or a tail jump to a function that returns."""
        f = self.found
        changed = True
        while changed:
            changed = False
            for entry, outs in f.tails.items():
                if entry not in f.returns and (None in outs or outs & f.returns):
                    f.returns.add(entry)
                    changed = True

    def _no_return(self, flat: int) -> bool:
        f = self.found
        return flat in f.takes_return or flat in self.hints.get("noreturn", ()) or (
            flat in self.prev_starts and flat not in f.returns)

    def _trace(self, entry: int, st: State) -> list[tuple[int, int, object, str]]:
        """Follow one function; returns the functions it calls or jumps to: (flat, cpu, the window's bank then,
        "call" | "jump")."""
        f, size = self.found, self.size
        out: list[tuple[int, int, object, str]] = []
        work = [(entry, st)]
        visited: set[int] = set()
        pushes, popped = 0, None
        while work:
            flat, st = work.pop()
            while flat not in visited and 0 <= flat < size and not self._hinted_data(flat):
                visited.add(flat)
                text, ln = self.decode(flat)
                if text.startswith("db ") or self.data[flat:flat + NOPS] == bytes(NOPS):
                    break  # (a run of zero bytes is padding, though it would run as nops)
                f.code[flat] = ln
                kind, target = sm83.flow(text)
                # the mapper, and what's kept in RAM
                if (w := store(st, text)) is not None:
                    reg, val = w
                    if reg < ROM_END and self.bank_register(reg):
                        if not (val == 1 and entry == ENTRY):  # (start-up code setting the power-on bank isn't paging)
                            f.mbc_writes += 1
                        new = self._bank_value(reg, val, st.bank)
                        if isinstance(val, tuple) and val[0] == "arg":  # the register it was given: callers pass a bank
                            f.bank_arg.setdefault(entry, val[1])
                            if val == ARG:
                                f.switchers.add(entry)
                            new = None
                        st.bank = new
                    elif _ram(reg):
                        st.mem[reg] = val if isinstance(val, (int, tuple)) else None
                        for lo in (reg - 1, reg):  # both bytes of a word known: maybe an address of code
                            word = (st.mem.get(lo), st.mem.get(lo + 1))
                            if all(isinstance(x, int) for x in word) and 0x100 <= (addr := word[0] | word[1] << 8) < ROM_END:
                                f.pointers.append((flat, addr, st.bank))
                            for pair, (hi, low) in PAIRS.items():  # the pair it was given: its callers' are pointers
                                if word == (("arg", low), ("arg", hi)):
                                    f.setters.setdefault(entry, set()).add(pair)
                if (m := _LOAD_A.match(text)) and isinstance(st.mem.get(_addr(m)), (int, tuple)):
                    st.r["a"] = st.mem[_addr(m)]  # a value it stored there itself (a bank kept for later)
                    text_loaded = True
                else:
                    text_loaded = False
                if text.startswith("push "):
                    pushes += 1
                    pair = text.split()[1]
                    if pair in PAIRS and (addr := st.pair(pair)) is not None and 0x100 <= addr < ROM_END:
                        f.pointers.append((flat, addr, st.bank))  # an address pushed to return to (`push hl / ret`)
                if (m := _POP_RET.match(text)) and pushes == 0 and len(visited) <= 3:
                    popped = m.group(1)  # its return address, first thing: if it reads through it, data follows calls
                elif popped and (_THROUGH[popped].search(text) or (popped == "hl" and text == "jp hl")):
                    f.takes_return.add(entry)
                    popped = None
                if text == "jp hl":
                    if st.r.get("h") == ("arg", "h") and st.r.get("l") == ("arg", "l"):
                        f.calls_arg.setdefault(entry, "hl")  # to the address it was given
                    if (hl := st.pair("hl")) is not None and hl < ROM_END:
                        kind, target = "jump", hl
                    else:
                        f.dispatchers.add(entry)
                        f.returns.add(entry)
                        if st.hl_base is not None and 0x100 <= st.hl_base < ROM_END:
                            f.indexed.append((flat, st.hl_base, st.bank, False))  # `ld hl,table` ... `jp hl`
                if kind in ("ret", "cret") and text != "jp hl":
                    f.returns.add(entry)
                if target is not None and kind in ("call", "jump", "cjump"):
                    tflat = self.resolve(target, flat, st.bank)
                    if tflat is None and kind != "call":
                        f.tails.setdefault(entry, set()).add(None)
                    if tflat is not None and flat < BANK <= target < 2 * BANK:
                        f.edges[flat] = tflat  # (which bank it reached is the follower's to know)
                    if tflat is not None:
                        inside = (tflat // BANK == entry // BANK and tflat != entry and tflat not in self.known)
                        if kind != "call" and tflat != entry:
                            f.jumps.append((entry, tflat, target))
                        if kind == "call" or not inside:
                            if tflat != entry:
                                out.append((tflat, target, st.bank, "call" if kind == "call" else "jump"))
                                if kind != "call":
                                    f.tails.setdefault(entry, set()).add(tflat)
                                if (hl := st.pair("hl")) is not None:
                                    f.with_hl.append((flat, hl, st.bank, tflat))
                        else:
                            work.append((tflat, st.copy()))
                    if kind == "call":
                        before = dict(st.r)
                        if tflat is not None:
                            tflat = self.through(tflat, st.bank)  # (what it is, not the vector it went through)
                        for pair in f.setters.get(tflat, ()) if tflat is not None else ():
                            if (addr := st.pair(pair)) is not None and 0x100 <= addr < ROM_END:
                                f.pointers.append((flat, addr, st.bank))
                        uncond = not text.startswith("call ") or "," not in text  # an unconditional call or rst
                        shape = self.inline_shape(tflat) if tflat is not None and uncond else None
                        if tflat is not None:
                            self._far(entry, flat, ln, tflat, before, st, shape, out)
                            keep = self.clobbers(tflat)
                            st.r = {k: v for k, v in st.r.items() if k not in keep}
                        else:
                            st.r = {}
                        if tflat is not None:
                            if (reg := f.bank_arg.get(tflat)):
                                st.bank = self._bank_value(SWITCH, before.get(reg), st.bank)
                                if isinstance(before.get(reg), tuple) and before[reg][0] == "arg":
                                    f.bank_arg.setdefault(entry, before[reg][1])  # it passes on a bank it was given
                                if st.bank == ARG or isinstance(st.bank, tuple):
                                    st.bank = None
                            if (pair := f.calls_arg.get(tflat)) and all(
                                    st.r.get(x, before.get(x)) == ("arg", x) for x in PAIRS[pair]):
                                f.calls_arg.setdefault(entry, pair)
                            if shape is not None:  # it returns past the bytes it reads: the code goes on after them
                                f.inline[flat + ln] = (shape.size, shape.why())
                                ln += shape.size
                            elif uncond:
                                if tflat in f.takes_return and tflat in f.dispatchers:
                                    f.indexed.append((flat, cpu_of(flat + ln), st.bank, True))  # `rst $28 / dw a, b`
                                if self._no_return(tflat):
                                    break
                elif not text_loaded:
                    step(st, text)
                if kind in ("ret", "jump"):
                    break
                nxt = flat + ln
                if nxt % BANK == 0:
                    break
                flat = nxt
        return out

    def _far(self, entry: int, flat: int, ln: int, callee: int, before: dict, st: State, shape: Inline | None,
             out: list) -> None:
        """A far call: to a routine that maps a bank it's given and jumps to an address it's given, in registers
        (`ld a,BANK / ld hl,Func / rst $08`) or in the bytes after the call (`call FarCall / db BANK / dw Func`). The
        function it reaches is followed with that bank."""
        f = self.found
        bank = addr = None
        guess = False
        if shape is not None and shape.addr is None and shape.bank is not None and shape.size == 3:
            shape, guess = Inline(3, shape.bank, 1 if shape.bank == 0 else 0), True  # a bank and the word beside it
        if shape is not None and shape.addr is not None:
            at = flat + ln
            addr = self.data[at + shape.addr] | self.data[at + shape.addr + 1] << 8 if at + shape.addr + 1 < self.size else None
            bank = self.data[at + shape.bank] if shape.bank is not None and at + shape.bank < self.size else st.bank
        elif (reg := f.bank_arg.get(callee)) and (pair := f.calls_arg.get(callee)):
            v = before.get(reg)
            hi, lo = (before.get(x) for x in PAIRS[pair])
            if isinstance(v, int) and isinstance(hi, int) and isinstance(lo, int):
                bank, addr = v, hi << 8 | lo
        if addr is None or addr >= ROM_END:
            return
        if addr >= BANK and isinstance(bank, int):
            bank = self._bank_value(SWITCH, bank, None) if self.kind != "none" else 1
        to = self.resolve(addr, 0 if addr >= BANK else flat, bank)  # (as code in bank 0 with that bank mapped would)
        if to is not None and to != entry and (not guess or self.plausible(to)):
            out.append((to, addr, bank if isinstance(bank, int) else None, "call"))
            if addr >= BANK:
                self.found.edges[flat] = to

    # -- what a round teaches the next

    def _far_jumps(self) -> dict[int, tuple[int, str]]:
        """Jump targets that are functions of their own: outside the jumping function's own bytes (up to the next
        function in the bank that something calls), or jumped to from more than one function (a shared tail)."""
        f = self.found
        anchors = sorted(f.anchors | set(f.starts) - {t for _e, t, _c in f.jumps})
        jumpers: dict[int, set] = {}
        for entry, tflat, _tcpu in f.jumps:
            jumpers.setdefault(tflat, set()).add(entry)
        out = {}
        for entry, tflat, tcpu in f.jumps:
            k = bisect.bisect_right(anchors, entry)
            end = min(anchors[k] if k < len(anchors) else self.size, (entry // BANK + 1) * BANK)
            if not entry <= tflat < end or len(jumpers[tflat]) > 1:
                out.setdefault(tflat, (tcpu, "jump"))
        return out

    def _tables(self) -> dict[int, tuple[int, str]]:
        """Handlers in jump tables: one handed to a dispatcher in HL (`ld hl,table / rst $28`), one a function indexes
        itself (`ld hl,table / add a,a / ... / jp hl`), one right after a call to a dispatcher that reads its return
        address. Every word up to the first that isn't a plausible function in the bank the code had mapped; the words
        are data."""
        f, out = self.found, {}
        for tflat, n in self.hints.get("tables", []):  # what a person or an agent established: no guessing its end
            for k in range(n):
                at = tflat + 2 * k
                if at + 1 >= self.size:
                    break
                word = self.data[at] | self.data[at + 1] << 8
                if (cflat := self.resolve(word, tflat, None)) is not None and self.can_start(cflat):
                    out.setdefault(cflat, (word, "table"))
            f.tables[tflat] = max(n, f.tables.get(tflat, 0))
        tables = [(fr, table, bank, False) for fr, table, bank, callee in f.with_hl if callee in f.dispatchers]
        for from_flat, table, bank, inline in tables + f.indexed:
            if table < 0x0100:
                continue
            tflat = self.resolve(table, from_flat, bank)
            if tflat is None or (tflat in f.code and not inline):
                continue
            n = 0
            for k in range(64):
                at = tflat + 2 * k
                if at + 1 >= self.size or (k and at % BANK == 0) or at in f.code or at + 1 in f.code:
                    break
                word = self.data[at] | self.data[at + 1] << 8
                cflat = self.resolve(word, from_flat, bank)
                if cflat is None or not self.plausible(cflat):
                    break
                out.setdefault(cflat, (word, "table"))
                n = k + 1
            if n:
                f.tables[tflat] = max(n, f.tables.get(tflat, 0))
        return out

    def _pointers(self) -> dict[int, tuple[int, str]]:
        """Addresses of code the code keeps: stored to RAM as a word (a state's handler: `ld hl,wState / ld (hl),
        LOW(fn) / inc hl / ld (hl), HIGH(fn)`), or pushed to return to. Kept when they read as a function's start."""
        out = {}
        for from_flat, addr, bank in self.found.pointers:
            flat = self.resolve(addr, from_flat, bank)
            if flat is not None and flat not in out and self.plausible(flat):
                out[flat] = (addr, "pointer")
        return out


def seeds(data: bytes, hints: dict | None = None) -> dict[int, tuple[int, str]]:
    out = {v: (v, "vector") for v in VECTORS if v < len(data)}
    out[ENTRY] = (ENTRY, "entry")
    for flat in (hints or {}).get("entries", {}):
        if 0 <= flat < len(data):
            out.setdefault(flat, (cpu_of(flat), "hint"))
    cov = (hints or {}).get("coverage")
    for flat in sorted(cov.entries) if cov is not None else ():  # where calls entered while the game ran
        if 0 <= flat < len(data):
            out.setdefault(flat, (cpu_of(flat), "ran"))
    return out


def follow(data: bytes, hints: dict | None = None, rounds: int = 3) -> Found:
    """The code reachable from the vectors and the entry point (and what `hints` add), followed with its banks. Then,
    a few times over, from the code nothing reached that calls two functions that were (code reached through pointers
    the following can't see): what that reaches, its far calls included, is followed too."""
    start = seeds(data, hints)
    probe = Tracer(data, hints=hints).follow(start)
    paged = probe.mbc_writes > 0
    found = Tracer(data, paged=paged, hints=hints).follow(start)
    for _ in range(rounds):
        more = {flat for flat in _sorted(data, found, hints)[2] if flat not in found.starts}
        if not more:
            break
        start = {**{flat: (cpu_of(flat), "evidence") for flat in more}, **start}
        found = Tracer(data, paged=paged, hints=hints).follow(start)
    cov = (hints or {}).get("coverage")
    if cov is not None:  # code that ran but wasn't followed (reached through a pointer): where each run of it starts
        ran = {flat: (cpu_of(flat), "ran") for flat in range(len(data)) if cov.code[flat]
               and (flat == 0 or not cov.code[flat - 1]) and flat not in found.code}
        if ran:
            found = Tracer(data, paged=paged, hints=hints).follow({**ran, **start})
    return found


# ---------------------------------------------------------------- what wasn't reached

_TERMINAL = ("ret", "jump")


def regions(data: bytes, found: Found, hints: dict | None = None) -> list[tuple[int, int, str, str]]:
    """(flat, size, kind, why) for the stretches that aren't code: the tables the code reads (`table`), the bytes after a
    call that the routine called reads (`inline`), what a person or an agent marked (`hint`), runs of padding
    (`padding`), and what the code never reached and reads as no code would (`data`). Unreached bytes that read
    like code are left out: mgbdis still decodes them, as before.

    Unreached bytes are cut into pieces as they decode from where the stretch begins (and from wherever code calls or
    jumps): straight-line code up to a return, a jump or a call to a routine that reads what follows its call. An
    instruction no code would run (bytes that are no instruction, `rst $38`: an FF byte, a register loaded with itself,
    a `stop` with no 00 after it, half an instruction running into reached code) is data, and so is the rest of its
    straight line of code, but for what in it shows a sign of being code: reached code goes there (or code-like code
    that calls two functions the code reached), or it calls two itself (one can be bytes of data by chance). So is a code-like piece shorter than 8 bytes between two of
    data that nothing goes to (graphics decode to the odd `ret`)."""
    return _sorted(data, found, hints)[0]


def _sorted(data: bytes, found: Found, hints: dict | None = None):
    """(regions, the unreached stretches' pieces, where code-like code with a sign of being code starts): see
    `regions`."""
    size = len(data)
    t = Tracer(data, hints=hints)
    t.found = found  # (what following the code learned: the routines that read bytes after their calls)
    out: list[tuple[int, int, str, str]] = []
    claimed = bytearray(size)  # 1: reached code, 2: a table, operands, a hint or the header
    for flat, ln in found.code.items():
        claimed[flat:flat + ln] = b"\x01" * min(ln, size - flat)
    for flat, (n, why) in found.inline.items():
        out.append((flat, n, "inline", why))
        claimed[flat:flat + n] = b"\x02" * min(n, size - flat)
    cov = (hints or {}).get("coverage")
    if cov is not None:  # what ran is code (never data); what was only read while the game ran is data
        for flat, n in cov.runs("code", 1):
            for k in range(flat, flat + n):
                if not claimed[k]:
                    claimed[k] = 1
        for flat, n in cov.runs("read"):
            n = next((k - flat for k in range(flat, flat + n) if claimed[k]), n)
            if n > 0:
                out.append((flat, n, "read", "read as data while the game ran (coverage)"))
                claimed[flat:flat + n] = b"\x02" * n
    for flat, n in found.tables.items():
        out.append((flat, 2 * n, "table", f"a table of {n} handler address{'es' if n != 1 else ''}"))
        claimed[flat:flat + 2 * n] = b"\x02" * min(2 * n, size - flat)
    for flat, n in (hints or {}).get("data", []):
        lo, hi = max(0, flat), min(size, flat + n)
        if hi > lo:
            out.append((lo, hi - lo, "hint", "marked as data"))
            claimed[lo:hi] = b"\x02" * (hi - lo)
    claimed[HEADER[0]:HEADER[1]] = b"\x02" * (HEADER[1] - HEADER[0])
    spans = []
    off = 0
    while off < size:
        if claimed[off]:
            off += 1
            continue
        end = off
        while end < size and not claimed[end] and (end == off or end % BANK):
            end += 1
        spans.append((off, end))
        off = end
    # what code (reached, or code-like) calls or jumps to starts a piece of its own: code often follows data with no
    # return between them. Twice more with what the code-like pieces call, as they come out once cut there.
    targets: set[int] = set()
    for flat in found.code:
        _target(t, flat, targets)
    sure = set(targets)  # where reached code goes, or code-like code that calls reached code
    for _ in range(3):
        cuts = sorted(targets)
        gaps = [_pieces(data, t, lo, hi, cuts, found) for lo, hi in spans]
        more = set(targets)
        for pieces in gaps:
            for lo, hi, kind, calls, _line in pieces:
                if kind == "code":
                    k = lo
                    while k < hi:
                        _target(t, k, more)
                        if calls >= SIGN:
                            _target(t, k, sure)
                        k += t.decode(k)[1]
        if more == targets:
            break
        targets = more
    for pieces in gaps:
        # a straight line of code with an instruction no code would run in it is data, but for what in it has a sign
        # of being code: reached code (or code-like code that calls reached code) goes there, or it calls reached code
        tainted = {p[4] for p in pieces if p[2] == "data"}
        for p in pieces:
            if p[2] == "code" and p[4] in tainted and p[0] not in sure and p[3] < SIGN:
                p[2] = "data"
        # and a short code-like piece between two of data, that nothing goes to (graphics decode to the odd `ret`)
        for k, p in enumerate(pieces):
            if p[2] == "code" and p[1] - p[0] < 8 and p[0] not in targets:
                before = pieces[k - 1][2] if k else "edge"
                after = pieces[k + 1][2] if k + 1 < len(pieces) else "edge"
                if (before == "data" and after in ("data", "edge")) or (before == "edge" and after == "data"):
                    p[2] = "data"
        for lo, hi, kind, _calls, _line in pieces:
            if kind == "inline":
                out.append((lo, hi - lo, "inline", "bytes the routine called reads after its call"))
            elif kind == "padding":
                out.append((lo, hi - lo, "padding", "padding (00 or FF bytes)"))
            elif kind == "data":
                if out and out[-1][2] == "data" and out[-1][0] + out[-1][1] == lo:
                    out[-1] = (out[-1][0], hi - out[-1][0], "data", out[-1][3])
                else:
                    out.append((lo, hi - lo, "data", "never reached, and reads as no code would"))
    evidence = set()
    for pieces in gaps:  # code-like code that calls two reached functions, where its straight line (or it) starts
        for k, p in enumerate(pieces):
            if p[2] == "code" and p[3] >= SIGN and (k == 0 or pieces[k - 1][4] != p[4] or pieces[k - 1][2] == "data"):
                if t.plausible(p[0]):
                    evidence.add(p[0])
    return sorted(out), gaps, evidence


def _target(t: Tracer, flat: int, into: set[int]) -> None:
    kind, target = sm83.flow(t.decode(flat)[0])
    if target is not None and kind in ("call", "jump", "cjump") and (tflat := t.resolve(target, flat, None)) is not None:
        into.add(tflat)


def _bad(data: bytes, off: int, text: str) -> bool:
    """An instruction no code would run: bytes that are no instruction, `rst $38` (an FF byte), a register loaded with
    itself, a `stop` with no 00 after it."""
    return text.startswith("db ") or text == "rst $38" or SELF_MOVE.match(text) is not None or (
        text.startswith("stop") and data[off + 1:off + 2] != b"\x00")


def _pieces(data: bytes, t: Tracer, lo: int, hi: int, cuts: list[int], found: Found) -> list[list]:
    """One unreached stretch cut into [lo, hi, "padding" | "data" | "code", calls reached code] pieces: straight-line
    code up to a return or a jump, a call to a routine that reads what follows its call, or an instruction no code would
    run (a piece of data of its own); a new one at each of `cuts` (where code goes)."""
    pieces: list[list] = []
    off, line, ended = lo, 0, False
    while off < hi:
        run = off
        while run < hi and data[run] == data[off] and data[off] in (0x00, 0xFF):
            run += 1
        if run - off >= 4:
            line += 1
            pieces.append([off, run, "padding", 0, line])
            off, ended = run, True
            continue
        if ended or off in cuts_set(cuts):
            line += 1  # a new straight line of code: after a return, a jump, padding, or where code goes
        ended = False
        start, calls, inline = off, set(), None
        k = bisect.bisect_right(cuts, off)
        stop = min(hi, cuts[k]) if k < len(cuts) else hi  # the next place code goes to
        text, ln = t.decode(off)
        if off + ln > hi or _bad(data, off, text):  # (half an instruction into reached code is no code either)
            pieces.append([off, min(off + ln, hi), "data", 0, line])
            off = min(off + ln, hi)
            continue
        while off < stop:
            text, ln = t.decode(off)
            if off + ln > hi or _bad(data, off, text):
                break  # the bad instruction is a piece of its own
            if off + ln > stop:  # where code goes is inside this instruction: not there after all
                k = bisect.bisect_right(cuts, off + ln - 1)
                stop = min(hi, cuts[k]) if k < len(cuts) else hi
            kind, target = sm83.flow(text)
            to = t.resolve(target, off, None) if target is not None else None
            if to is not None and kind in ("call", "jump", "cjump") and to in found.starts and ln == 3:
                calls.add(to)  # (not an rst: one byte, which data holds all the time)
            off += ln
            callee = t.through(to, None) if kind == "call" and to is not None else None
            if callee is not None and "," not in text and (shape := t.inline_shape(callee)) is not None:
                inline = shape
                break
            if kind in _TERMINAL or (callee is not None and callee in found.takes_return):
                ended = True
                break
            if off < stop and data[off] in (0x00, 0xFF) and data[off:off + 4] == bytes([data[off]]) * 4:
                break  # padding follows
        pieces.append([start, off, "code", len(calls), line])
        if inline is not None and off + inline.size <= hi:  # the bytes the routine it called reads: then code again
            pieces.append([off, off + inline.size, "inline", 0, line])
            off += inline.size
    return pieces


_CUTS: dict[int, frozenset] = {}


def cuts_set(cuts: list[int]) -> frozenset:
    key = id(cuts)
    if key not in _CUTS:
        _CUTS.clear()
        _CUTS[key] = frozenset(cuts)
    return _CUTS[key]


# ---------------------------------------------------------------- for mgbdis

def label_for(flat: int, how: str) -> str:
    """mgbdis's own naming, for a start the follower found: `Call_` when something calls it, `Func_` when it's only
    jumped to (mgbdis would call it `Jump_`, which the split takes for a branch inside a function)."""
    return f"{'Func' if how == 'jump' else 'Call'}_{flat // BANK:03x}_{cpu_of(flat):04x}"


_DEFAULT_LABELS = set(VECTORS) | {ENTRY}


def sym_text(data: bytes, found: Found, regs: list[tuple[int, int, str, str]]) -> str:
    """A symbol file for mgbdis: a label and a `.code` block at every function start the follower found (so mgbdis
    decodes from there, whatever came before), and a `.data` block for every region."""
    lines = ["; made by decomp.wtf from following the game's code (rom/gb_trace.py)"]
    bounds = sorted({f for f, *_ in regs} | {f + n for f, n, *_ in regs} | set(found.starts))
    for flat, (_cpu, how) in sorted(found.starts.items()):
        if flat in _DEFAULT_LABELS or HEADER[0] <= flat < HEADER[1]:
            continue
        k = bisect.bisect_right(bounds, flat)
        end = min(bounds[k] if k < len(bounds) else len(data), (flat // BANK + 1) * BANK)
        lines.append(f"{flat // BANK:02x}:{cpu_of(flat):04x} {label_for(flat, how)}")
        lines.append(f"{flat // BANK:02x}:{cpu_of(flat):04x} .code:{end - flat:x}")
    for flat, n, _kind, _why in regs:
        while n > 0:  # a block stays in its bank
            take = min(n, (flat // BANK + 1) * BANK - flat)
            lines.append(f"{flat // BANK:02x}:{cpu_of(flat):04x} .data:{take:x}")
            flat, n = flat + take, n - take
    return "\n".join(lines) + "\n"
