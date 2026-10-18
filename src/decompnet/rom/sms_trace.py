"""Following a Master System / Game Gear game's code with the banks it maps, to find its functions and the CPU address
each runs at.

The Sega mapper shows 16 KiB banks of the ROM in three slots (0x0000, 0x4000, 0x8000) and code picks the banks of
slots 1 and 2 by writing their number to 0xFFFE and 0xFFFF. Which bank a call to 0x8123 reaches, and where a bank's
code runs, isn't in the ROM's layout: it's in the code. So the code is followed from the reset, interrupt and pause
entry points, instruction by instruction, keeping track of what's in each slot:

- `ld a,5 / ld ($ffff),a` (and through HL: `ld hl,$ffff / ld (hl),5`) maps a bank; the mapper reads only as many
  bits as the ROM needs;
- a function that writes the A it was called with to the mapper maps the bank its callers pass in A;
- a jump or call into the slot the code runs from stays in its own bank;
- a function that never returns (a main loop, a reset) or that takes its return address to read a table that
  follows its call (`call dispatch / .dw a, b, c`, or `ex (sp),hl` first thing) ends the straight-line code at the
  call; one that ends in a jump to a function that returns returns too;
- slot 0 above 0x0400 is paged by writes to 0xFFFD; a bank kept in RAM (`ld a,5 / ld (shadow),a` ...
  `ld a,(shadow) / ld ($ffff),a`), read back from the mapper's mirror (`ld a,($ffff)`) or saved on the stack
  (`push af` ... `pop af`) is followed through;
- a computed call (`ld de,.next / push de / jp (hl)`) comes back to the address it pushed, and `push rr / ret` is a
  jump to it.

Rounds repeat that with what the last one learned (functions that don't return, bank switchers) until nothing
changes. Code tables seed more functions: the ones a game reads through a dispatcher (`ld hl,table / rst $20`), and
the ones it indexes itself before `jp (hl)` (`ld hl,table / add hl,de / ... / jp (hl)`, a table of words or of `jp`
instructions), as many entries as the index is bounded to (`cp 5 / jr nc` before it) or else up to the first that
doesn't read like code. Labels from a WLA-DX symbol file next to the ROM (emulators and disassemblers write them)
seed more, their automatic `_LABEL_..._` names among them.
(Ideas from another fork of the original, rewritten for this lane's own disassembler and records.)
"""

# Portions ported from SynapticDreams; Copyright (c) 2026 Sam Wolfs; MIT License (see licenses/synaptic-sms-MIT.txt).
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from . import z80

BANK = 0x4000
VECTORS = (0x0000, 0x0008, 0x0010, 0x0018, 0x0020, 0x0028, 0x0030, 0x0038, 0x0066)
ARG = "arg"  # A as the function was entered with it: a function that maps it is a bank switcher

_LD_A_N = re.compile(r"^ld a, \$([0-9a-f]{2})$")
_LD_A_MEM = re.compile(r"^ld a, \(\$([0-9a-f]{4})\)$")
_LD_MEM_A = re.compile(r"^ld \(\$([0-9a-f]{4})\), a$")
_LD_HL_NN = re.compile(r"^ld hl, \$([0-9a-f]{4})$")
_LD_RR_NN = re.compile(r"^ld (hl|de|bc), \$([0-9a-f]{4})$")
_LD_HLP_N = re.compile(r"^ld \(hl\), \$([0-9a-f]{2})$")
_CP_N = re.compile(r"^cp \$([0-9a-f]{2})$")
_AND_N = re.compile(r"^and \$([0-9a-f]{2})$")
_SUB_N = re.compile(r"^sub \$([0-9a-f]{2})$")
# what changes A's value (so an index bounded before isn't any more): not `or a`/`and a` (tests), not doubling it
_CHANGES_A = re.compile(r"^(?:add a, (?!a$)|adc a,|sub |sbc a,|inc a$|dec a$|neg|cpl|or (?!a$)|xor |and (?!a$)|rra|"
                        r"rla|rrca|srl a|sra a|rr a|rl a|rrc a|daa|ld a,|in a,|pop af|ex af)")
MAPPER = {0xFFFD: 0, 0xFFFE: 1, 0xFFFF: 2}  # the Sega mapper's bank registers -> the slot each pages
FRESH = 16  # instructions within which a bound or a table base belongs to the dispatch that follows it
_WRITES_DE = re.compile(r"^(?:ld de,|ld d,|ld e,|pop de|exx|inc de|dec de|inc d$|dec d$|inc e$|dec e$|ldi|ldd|ldir|"
                        r"lddr|ex de, hl|rlc [de]$|rrc [de]$|rl [de]$|rr [de]$|sla [de]$|sra [de]$|srl [de]$)")
_WRITES_BC = re.compile(r"^(?:ld bc,|ld b,|ld c,|pop bc|exx|inc bc|dec bc|inc b$|dec b$|inc c$|dec c$|djnz|ldi|ldd|"
                        r"ldir|lddr|cpi|cpd|cpir|cpdr|ini|ind|inir|indr|outi|outd|otir|otdr|in [bc],|"
                        r"rlc [bc]$|rrc [bc]$|rl [bc]$|rr [bc]$|sla [bc]$|sra [bc]$|srl [bc]$)")
# what else changes A or HL (so what was known about them isn't any more)
_WRITES_A = re.compile(r"^(?:ld a,|in a,|inc a$|dec a$|add a,|adc a,|sub |sbc a,|and |xor |or |rlca|rrca|rla|rra|"
                       r"cpl|neg|daa|pop af|ex af|rlc a|rrc a|rl a|rr a|sla a|sra a|sll a|srl a|res \d, a|set \d, a)")
_WRITES_HL = re.compile(r"^(?:ld hl,|ld h,|ld l,|pop hl|ex de, hl|exx|add hl,|adc hl,|sbc hl,|ex \(sp\), hl|inc h$|"
                        r"dec h$|inc l$|dec l$|ldi|ldd|ldir|lddr|cpi|cpd|cpir|cpdr|ini|ind|inir|indr|outi|outd|otir|otdr|"
                        r"rlc [hl]$|rrc [hl]$|rl [hl]$|rr [hl]$|sla [hl]$|sra [hl]$|srl [hl]$)")


def bank_mask(size: int) -> int:
    """The mapper reads only as many bank bits as the ROM needs (`ld a,$82 / ld ($ffff),a` maps bank 2 of 512 KiB)."""
    banks = 1
    while banks * BANK < size:
        banks <<= 1
    return banks - 1


def resolve(target: int, here_flat: int, here_cpu: int, s1, s2, size: int, s0=0) -> int | None:
    """The ROM offset a call or jump to CPU address `target` reaches from code at `here_flat` (running at
    `here_cpu`) while slots 1 and 2 hold banks `s1` and `s2`, and slot 0 above 0x0400 bank `s0` (None: not known).
    None for RAM, an unknown bank, or past the ROM's end."""
    if target >= 0xC000:
        return None
    if target < 0x4000:
        if target < 0x0400:
            return target if target < size else None  # (the first KiB is always bank 0)
        bank = here_flat // BANK if here_cpu < 0x4000 and here_flat >= BANK else s0  # (code paged into slot 0)
        if not isinstance(bank, int):
            return None
        flat = bank * BANK + target
        return flat if flat < size else None
    slot = target >> 14
    if here_cpu >= 0x4000 and here_cpu >> 14 == slot:
        bank = here_flat // BANK  # into the slot the code runs from: its own bank
    else:
        bank = s1 if slot == 1 else s2
    if not isinstance(bank, int):
        return None
    flat = bank * BANK + (target & (BANK - 1))
    return flat if flat < size else None


@dataclass
class State:
    s1: object
    s2: object
    a: object = None   # an int, ARG, or None
    hl: int | None = None
    s0: object = 0  # slot 0's bank above 0x0400 (0 unless the code pages it: 0xFFFD)
    de: int | None = None
    bc: int | None = None
    mem: dict = field(default_factory=dict)  # RAM this function stored a known value to (a bank kept in RAM)
    stack: list = field(default_factory=list)  # what it pushed: values (an int, ARG or None)
    n: int = 0  # instructions so far on this path
    cmp: tuple | None = None  # (n, the number A was just compared with)
    low: tuple | None = None  # (n, the least A can be here: `cp 5 / jr c` passed)
    bound: tuple | None = None  # (n, how many values A can have here, exactly so: `cp 5 / jr nc` (True) or at most:
    # `and $7f` (False)): an index
    index: tuple | None = None  # the same, for the index once A is copied into another register (`ld e, a`)
    base: tuple | None = None  # (n, the last ROM address loaded into a register pair: a table to index)

    def copy(self) -> State:
        return State(self.s1, self.s2, self.a, self.hl, self.s0, self.de, self.bc, dict(self.mem), list(self.stack),
                     self.n, self.cmp, self.low, self.bound, self.base, self.index)

    def fresh(self, mark: tuple | None):
        """What a comparison, a bound or a table base says, while it still belongs to what follows it."""
        if mark is None or self.n - mark[0] > FRESH:
            return None
        return mark[1] if len(mark) == 2 else mark[1:]


@dataclass
class Found:
    starts: dict[int, tuple[int, str]] = field(default_factory=dict)  # flat -> (cpu it runs at, how it was found)
    entered: dict[int, set] = field(default_factory=dict)  # flat -> every (s1, s2) it was entered with
    returns: set[int] = field(default_factory=set)  # functions a path of which reaches a return
    takes_return: set[int] = field(default_factory=set)  # functions that pop their return address (a table follows)
    switchers: dict[int, int] = field(default_factory=dict)  # function -> the slot it maps the A it's given into
    dispatchers: set[int] = field(default_factory=set)  # functions that jump through HL to an address they read
    # calls and tail jumps made with a known address in HL: (from flat, from cpu, HL, the slots then, to flat, the
    # index's bound or None): a table, when what they go to is a dispatcher
    with_hl: list[tuple[int, int, int, tuple, int, int | None]] = field(default_factory=list)
    # tables a function indexes itself before `jp (hl)`: (from flat, from cpu, table, the slots then, bound or None)
    indexed: list[tuple[int, int, int, tuple, int | None]] = field(default_factory=list)
    tails: dict[int, set] = field(default_factory=dict)  # function -> where its tail jumps go (None: not known)
    code: dict[int, int] = field(default_factory=dict)  # every instruction followed: flat -> its length
    jumps: list[tuple[int, int, int, tuple]] = field(default_factory=list)  # (function, to flat, to cpu, slots)
    anchors: set[int] = field(default_factory=set)  # starts reached by something other than a jump (a call...)
    slot_writes: dict[int, int] = field(default_factory=dict)  # 1 | 2 -> mapper writes seen that pick that slot's bank
    bank_base: dict[int, int] = field(default_factory=dict)  # bank -> the CPU address its code was found running at


def _decode(data: bytes, flat: int, cpu: int) -> tuple[str, int]:
    end = min(len(data), (flat // BANK + 1) * BANK)
    return z80.decode(data[flat:min(end, flat + 4)], cpu)


def _agreed(mappings: set) -> tuple:
    s1 = {m[0] for m in mappings}
    s2 = {m[1] for m in mappings}
    return (s1.pop() if len(s1) == 1 else None), (s2.pop() if len(s2) == 1 else None)


def can_start(data: bytes, flat: int, cpu: int) -> bool:
    """Where a call or jump can land: an instruction, and not the $FF filler of an empty bank (`rst $38` again and
    again)."""
    if not 0 <= flat < len(data) or data[flat:flat + 2] == b"\xff\xff":
        return False
    return not _decode(data, flat, cpu)[0].startswith("db ")


_ODD_START = re.compile(r"^(?:nop|ld \(bc\), a|ld \(de\), a|ex af|halt|rst |daa|ccf|scf|di$|ei$|ld ([a-e]), \1$)")


def plausible(data: bytes, flat: int, cpu: int, limit: int = 64) -> bool:
    """For an address that's only a guess (a word in a table): code that reads like a function's start: no filler or
    undefined opcodes, not what tables of numbers decode to first, and a return or jump within `limit` instructions
    in the bank."""
    if not can_start(data, flat, cpu) or data[flat:flat + 2] == b"\x00\x00":
        return False
    for k in range(limit):
        text, ln = _decode(data, flat, cpu)
        if text.startswith("db ") or text == "rst $38" or (k == 0 and _ODD_START.match(text)):
            return False
        if z80.flow(text)[0] in ("ret", "jump"):
            return True
        flat, cpu = flat + max(1, ln), cpu + max(1, ln)
        if flat % BANK == 0:
            return False
    return False


class Tracer:
    def __init__(self, data: bytes, paged: set[int] | None = None, hints: dict | None = None):
        """`paged`: the slots (1, 2) the game is known to pick banks for (a first pass learns it: `slot_writes`). Code
        entered from somewhere nothing was followed from (the interrupt handlers, what a sweep found) can't know
        what those slots hold, so their bank is unknown there; a slot the game never pages keeps its power-on bank.
        None: every slot keeps its power-on bank (the first pass's view). `hints`: the project's corrections (see
        sms.functions): bytes that aren't code, routines that read bytes after their call or never return."""
        self.data, self.size = data, len(data)
        self.hints = hints or {}
        cov = self.hints.get("coverage")  # (what an emulator only saw read is data too)
        self.data_spans: list[tuple[int, int]] = []  # merged, in order
        for a, n in sorted(list(self.hints.get("data") or []) + (cov.runs("read") if cov is not None else [])):
            if self.data_spans and a <= sum(self.data_spans[-1]):
                last = self.data_spans[-1]
                self.data_spans[-1] = (last[0], max(sum(last), a + n) - last[0])
            else:
                self.data_spans.append((a, n))
        self.mask = bank_mask(self.size)
        self.mapper = self.size > 3 * BANK  # 48 KiB or less: the ROM is simply at 0x0000-0xBFFF
        self.unknown = (None if paged and 1 in paged else 1, None if paged and 2 in paged else 2)
        self.found = Found()
        self.known: set[int] = set()  # function starts as the last round left them (and the seeds)
        self.prev_starts: set[int] = set()

    def follow(self, seeds: dict[int, tuple[int, str]], rounds: int = 8) -> Found:
        """The functions reachable from `seeds` (flat -> (cpu, why)), in rounds until nothing changes: each round
        starts again from the seeds with what the last one learned (far jumps and tables that start functions,
        functions that don't return, bank switchers). The reset vector starts with the power-on banks; any other
        seed with what its callers had mapped, as far as they agree (else `unknown`)."""
        more: dict[int, tuple[int, str]] = {}  # far jumps and tables, as the last round found them
        before = None
        for _ in range(rounds):
            todo = {**more, **seeds}
            self.known = set(todo)
            self.prev_starts = set(self.found.starts)
            self._round(todo)
            self._returning()
            more = {**self._far_jumps(), **self._tables()}
            now = (frozenset(self.found.starts.items()), frozenset(self.found.switchers.items()),
                   frozenset(self.found.returns), frozenset(self.found.takes_return), frozenset(more))
            if now == before:
                break
            before = now
        self._bank_bases()
        return self.found

    # -- one round

    def _round(self, seeds: dict[int, tuple[int, str]]) -> None:
        f = self.found
        prev, f.entered, f.starts, f.with_hl, f.jumps, f.anchors = f.entered, {}, {}, [], [], set()
        f.indexed, f.tails, f.code = [], {}, {}
        queue = [(flat, cpu, why, (1, 2) if flat == 0 else None) for flat, (cpu, why) in sorted(seeds.items())]
        for flat, _cpu, _why, m in queue:
            if m is not None:
                f.entered.setdefault(flat, set()).add(m)
        seen: set[int] = set()
        while queue:
            flat, cpu, why, mapping = queue.pop()
            if why != "jump" and can_start(self.data, flat, cpu):
                f.anchors.add(flat)
            if flat in seen or not can_start(self.data, flat, cpu) or self.in_data(flat):
                continue
            seen.add(flat)
            f.starts.setdefault(flat, (cpu, why))
            # a slot's bank holds inside a function only when everything that enters it agrees on it
            entries = prev.get(flat, set()) | ({mapping} if mapping is not None else set())
            s1, s2 = (1, 2) if not self.mapper else _agreed(entries) if entries else self.unknown
            for cflat, ccpu, m, how in self._trace(flat, cpu, State(s1, s2, ARG)):
                f.entered.setdefault(cflat, set()).add(m)
                queue.append((cflat, ccpu, how, m))

    def _slot_value(self, val):
        return val & self.mask if isinstance(val, int) else None

    def _returning(self) -> None:
        """A function returns when a path of it does: a return, or a tail jump to a function that returns or to where
        no one can tell (another bank nobody knows, an address worked out at run time: what it reaches returns)."""
        f = self.found
        changed = True
        while changed:
            changed = False
            for entry, outs in f.tails.items():
                if entry not in f.returns and (None in outs or outs & f.returns):
                    f.returns.add(entry)
                    changed = True

    def _no_return(self, flat: int) -> bool:
        """A call to it doesn't come back to the next instruction (as the last round saw it)."""
        f = self.found
        return flat in f.takes_return or flat in self.hints.get("noreturn", ()) or (
            flat in self.prev_starts and flat not in f.returns)

    def in_data(self, flat: int) -> bool:
        """In bytes a correction (or coverage) says aren't code."""
        k = bisect.bisect_right(self.data_spans, (flat, 1 << 30)) - 1
        return k >= 0 and self.data_spans[k][0] <= flat < sum(self.data_spans[k])

    def _trace(self, entry: int, entry_cpu: int, st: State) -> list[tuple[int, int, tuple, str]]:
        """Follow one function; returns the functions it calls or jumps to: (flat, cpu, the banks mapped then,
        "call" | "jump")."""
        f, data, size = self.found, self.data, self.size
        out: list[tuple[int, int, tuple, str]] = []
        tails = f.tails.setdefault(entry, set())
        work = [(entry, entry_cpu, st)]
        visited: set[int] = set()
        pushes = 0
        while work:
            flat, cpu, st = work.pop()
            while flat not in visited and 0 <= flat < size and not self.in_data(flat):
                visited.add(flat)
                text, ln = _decode(data, flat, cpu)
                if text.startswith("db "):
                    break
                f.code[flat] = ln
                st.n += 1
                skip = 0  # bytes after a call that the routine called reads (a correction says how many)
                kind, target = z80.flow(text)
                # the mapper (and its mirror in RAM, which reads back the bank last mapped)
                slot_reg, val = None, None
                if (m := _LD_MEM_A.match(text)) and int(m.group(1), 16) in MAPPER:
                    slot_reg, val = int(m.group(1), 16), st.a
                elif text == "ld (hl), a" and st.hl in MAPPER:
                    slot_reg, val = st.hl, st.a
                elif (m := _LD_HLP_N.match(text)) and st.hl in MAPPER:
                    slot_reg, val = st.hl, int(m.group(1), 16)
                if slot_reg is not None:
                    slot = MAPPER[slot_reg]
                    if not (slot == 1 and val == 1) and not (slot == 0 and val == 0):  # (power-on banks: not paging)
                        f.slot_writes[slot] = f.slot_writes.get(slot, 0) + 1
                    if val == ARG:
                        f.switchers.setdefault(entry, slot)
                    self._map(st, slot, val)
                    st.mem[slot_reg] = st.mem[slot_reg - 0x2000] = val
                elif (m := _LD_MEM_A.match(text)) and 0xC000 <= int(m.group(1), 16) < 0x10000:
                    st.mem[int(m.group(1), 16)] = st.a  # (a bank kept in RAM, say)
                elif text == "ld (hl), a" and st.hl is not None and st.hl >= 0xC000:
                    st.mem[st.hl] = st.a
                elif (m := _LD_HLP_N.match(text)) and st.hl is not None and st.hl >= 0xC000:
                    st.mem[st.hl] = int(m.group(1), 16)
                if text.startswith("push "):
                    pushes += 1
                    reg = text[5:]
                    st.stack.append(st.a if reg == "af" else st.hl if reg == "hl" else st.de if reg == "de"
                                    else st.bc if reg == "bc" else None)
                if (text == "pop hl" or text.startswith("ex (sp), ")) and pushes == 0 and len(visited) <= 3:
                    f.takes_return.add(entry)  # its return address, first thing: the table that follows its call
                computed = None  # a computed call: the address pushed to come back to (`push de / jp (hl)`)
                if text in ("jp (hl)", "jp (ix)", "jp (iy)"):
                    if st.stack and isinstance(st.stack[-1], int):
                        computed = st.stack.pop()
                    to = st.hl if text == "jp (hl)" else None
                    tflat = resolve(to, flat, cpu, st.s1, st.s2, size, st.s0) if to is not None else None
                    if to is None:
                        f.dispatchers.add(entry)  # to an address it worked out: a dispatcher
                        base = st.fresh(st.base)
                        if base is not None and computed is None:  # a table it indexed itself
                            f.indexed.append((flat, cpu, base, (st.s1, st.s2), st.fresh(st.index) or st.fresh(st.bound)))
                    elif tflat is not None and tflat != entry:
                        out.append((tflat, to, (st.s1, st.s2), "call" if computed is not None else "jump"))
                    if computed is None:
                        tails.add(tflat if to is not None else None)
                elif text == "ret" and st.stack and isinstance(st.stack[-1], int) and pushes:
                    to = st.stack.pop()  # `push rr / ret`: a jump to what it pushed, not a return
                    tflat = resolve(to, flat, cpu, st.s1, st.s2, size, st.s0)
                    if tflat is not None and tflat != entry:
                        out.append((tflat, to, (st.s1, st.s2), "jump"))
                        f.jumps.append((entry, tflat, to, (st.s1, st.s2)))
                    tails.add(tflat)
                    break
                elif kind in ("ret", "cret"):
                    f.returns.add(entry)
                if target is not None and kind in ("call", "jump", "cjump"):
                    tflat = resolve(target, flat, cpu, st.s1, st.s2, size, st.s0)
                    if tflat is not None:
                        inside = (tflat // BANK == entry // BANK and target >> 14 == cpu >> 14 and tflat != entry
                                  and tflat not in self.known)
                        if kind != "call" and tflat != entry:  # every jump: the next round sees which are far
                            f.jumps.append((entry, tflat, target, (st.s1, st.s2)))
                        if kind == "call" or not inside:
                            if tflat != entry:  # a call, or a jump to another function: a tail call
                                out.append((tflat, target, (st.s1, st.s2), "call" if kind == "call" else "jump"))
                                if st.hl is not None:
                                    f.with_hl.append((flat, cpu, st.hl, (st.s1, st.s2), tflat, st.fresh(st.bound)))
                                if kind == "jump":
                                    tails.add(tflat)
                        else:  # a branch within the function
                            work.append((tflat, target, self._branch(st, text)))
                    elif kind == "jump":
                        tails.add(None)  # into a bank no one knows, or RAM
                    if kind == "call":
                        a_before = st.a
                        st.a, st.hl, st.de, st.bc, st.cmp = None, None, None, None, None  # (the index's bound
                        # stays: a helper may do the indexing for it)
                        st.mem = {k: v for k, v in st.mem.items() if k in MAPPER or k - 0x2000 in MAPPER}
                        if tflat is not None:
                            if tflat in f.switchers:
                                self._map(st, f.switchers[tflat], a_before)
                                reg = {0: 0xFFFD, 1: 0xFFFE, 2: 0xFFFF}[f.switchers[tflat]]
                                st.mem[reg] = st.mem[reg - 0x2000] = a_before
                            if not text.startswith("call ") or "," not in text:  # (an unconditional call)
                                # it comes back past the bytes a correction says it reads, or doesn't come back here
                                # (what follows the call isn't this code)
                                skip = (self.hints.get("inline") or {}).get(tflat, 0)
                                if not skip and self._no_return(tflat):
                                    break
                if kind != "call":
                    self._registers(st, text)
                if computed is not None:  # back from the computed call: on at the address it pushed
                    back = resolve(computed, flat, cpu, st.s1, st.s2, size, st.s0)
                    if back is not None and back // BANK == entry // BANK:
                        nxt = st.copy()
                        nxt.a, nxt.hl, nxt.de, nxt.bc = None, None, None, None
                        work.append((back, computed, nxt))
                    break
                if kind in ("ret", "jump"):
                    break
                if kind in ("cjump", "cret"):  # what the condition says on the way on (`cp 5 / jr nc`: A < 5)
                    self._condition(st, text, taken=False)
                nxt = flat + max(1, ln) + skip
                if nxt // BANK != flat // BANK:
                    break  # ran off the end of the bank
                flat, cpu = nxt, cpu + max(1, ln) + skip
        return out

    def _map(self, st: State, slot: int, val) -> None:
        if slot == 0:
            st.s0 = self._slot_value(val)
        elif slot == 1:
            st.s1 = self._slot_value(val)
        else:
            st.s2 = self._slot_value(val)

    def _branch(self, st: State, text: str) -> State:
        out = st.copy()
        self._condition(out, text, taken=True)
        return out

    @staticmethod
    def _condition(st: State, text: str, taken: bool) -> None:
        """After `cp n`, the carry says A < n: the path where it's set has A bounded to n values (an index); the
        other has A at least n."""
        cond = text.split(None, 1)[1].split(",")[0].strip() if " " in text else ""
        n = st.fresh(st.cmp)
        if n is None or cond not in ("c", "nc"):
            return
        if (cond == "c") == taken:
            st.bound = (st.n, n, True)
        else:
            st.low = (st.n, n)

    def _registers(self, st: State, text: str) -> None:
        """What an instruction does to what's known about A, HL, DE, BC, the stack and the flags."""
        if m := _CP_N.match(text):
            st.cmp = (st.n, int(m.group(1), 16) or None)
        elif (m := _AND_N.match(text)) and ((v := int(m.group(1), 16)) + 1) & v == 0 and v:
            st.bound = (st.n, v + 1, False)  # `and 7`: one of 8 at most (a table may be shorter)
        elif (m := _SUB_N.match(text)) and (b := st.fresh(st.bound)) and b[1] and (lo := st.fresh(st.low)) is not None \
                and lo >= int(m.group(1), 16):
            k = int(m.group(1), 16)  # `cp 3 / jr c / cp 8 / jr nc / sub 3`: one of 5
            st.bound, st.low = (st.n, b[0] - k, True), (st.n, lo - k)
        elif _CHANGES_A.match(text):
            st.bound = st.low = None
        elif re.match(r"^ld [bcdehl], a$", text) and (b := st.fresh(st.bound)):
            st.index = (st.n, *b)  # the index goes on in another register: what A does next doesn't change it
        if text == "pop af":
            st.a = st.stack.pop() if st.stack else None
            return
        if text in ("pop hl", "pop de", "pop bc"):
            val = st.stack.pop() if st.stack else None
            reg = text[4:]
            st.hl, st.de, st.bc = (val if reg == "hl" else st.hl, val if reg == "de" else st.de,
                                   val if reg == "bc" else st.bc)
            return
        if text == "ex de, hl":
            st.de, st.hl = st.hl, st.de
            return
        if m := _LD_A_N.match(text):
            st.a = int(m.group(1), 16)
        elif text == "xor a":
            st.a = 0
        elif m := _LD_A_MEM.match(text):
            addr = int(m.group(1), 16)
            st.a = st.mem.get(addr, self._mirror(st, addr))
        elif text == "ld a, (hl)" and st.hl is not None and st.hl >= 0xC000:
            st.a = st.mem.get(st.hl, self._mirror(st, st.hl))
        elif _WRITES_A.match(text):
            st.a = None
        if m := _LD_RR_NN.match(text):
            value = int(m.group(2), 16)
            setattr(st, m.group(1), value)
            if 0x0100 <= value < 0xC000:
                st.base = (st.n, value)
        else:
            if text == "inc hl" and st.hl is not None:
                st.hl = (st.hl + 1) & 0xFFFF
            elif text == "dec hl" and st.hl is not None:
                st.hl = (st.hl - 1) & 0xFFFF
            elif _WRITES_HL.match(text):
                st.hl = None
            if _WRITES_DE.match(text):
                st.de = None
            if _WRITES_BC.match(text):
                st.bc = None

    @staticmethod
    def _mirror(st: State, addr: int):
        """A mapper register, or its mirror in RAM, read back: the bank last mapped there, if known."""
        slot = MAPPER.get(addr, MAPPER.get(addr + 0x2000) if 0xDFFD <= addr <= 0xDFFF else None)
        return None if slot is None else (st.s0, st.s1, st.s2)[slot]

    # -- what a round teaches the next

    def _far_jumps(self) -> dict[int, tuple[int, str]]:
        """Jump targets that are functions of their own: outside the jumping function's own bytes (its start up to
        the next function in the bank that something calls), or jumped to from more than one function (a shared
        tail, a dispatcher). Worked out afresh each round from every jump, so a guess an earlier round made from
        code it took for another function's doesn't stay."""
        f = self.found
        anchors = sorted(f.anchors | set(f.starts) - {t for _e, t, _c, _m in f.jumps})
        jumpers: dict[int, set] = {}
        for entry, tflat, _tcpu, _m in f.jumps:
            jumpers.setdefault(tflat, set()).add(entry)
        out = {}
        for entry, tflat, tcpu, _m in f.jumps:
            k = bisect.bisect_right(anchors, entry)
            end = min(anchors[k] if k < len(anchors) else self.size, (entry // BANK + 1) * BANK)
            if not entry <= tflat < end or len(jumpers[tflat]) > 1:
                out.setdefault(tflat, (tcpu, "jump"))
        return out

    def _tables(self) -> dict[int, tuple[int, str]]:
        """Code addresses in tables: the ones a dispatcher is given in HL (`ld hl,table / jp dispatch`, or `rst
        $20`), and the ones a function indexes itself before `jp (hl)`."""
        f, out = self.found, {}
        for from_flat, from_cpu, table, slots, callee, bound in f.with_hl:
            if callee in f.dispatchers and table >= 0x0100:
                out.update({k: v for k, v in self._table(from_flat, from_cpu, table, slots, bound).items() if k not in out})
        for from_flat, from_cpu, table, slots, bound in f.indexed:
            out.update({k: v for k, v in self._table(from_flat, from_cpu, table, slots, bound).items() if k not in out})
        return out

    def _table(self, from_flat: int, from_cpu: int, table: int, slots: tuple, bound: tuple | None) -> dict:
        """The functions a table holds, in the bank its reader had mapped: words, or `jp` instructions (a table of
        jumps, three bytes each). With the index bounded exactly (`cp 5 / jr nc`), that many entries (one that
        isn't code is a gap: no handler there); else up to the first that doesn't read like a function's start, and
        no more than a mask allows (`and $7f`)."""
        s1, s2 = slots
        tflat = resolve(table, from_flat, from_cpu, s1, s2, self.size)
        out: dict[int, tuple[int, str]] = {}
        if tflat is None:
            return out
        limit, exact = bound if bound else (None, False)
        count = limit if limit and limit <= 128 else 128
        capped = bool(limit) and not exact  # (a mask's ceiling: one that isn't code is skipped, not the end)
        bound = limit if exact and limit <= 128 else None
        if self.data[tflat] == 0xC3:  # jp nnnn, jp nnnn, ...: each entry is code, the jump is its own
            for k in range(count):
                at = tflat + 3 * k
                if at + 2 >= self.size or self.data[at] != 0xC3 or (k and at // BANK != tflat // BANK):
                    break
                out[at] = (table + 3 * k, "table")
            return out
        lowest = None  # the lowest handler past the table: between the table and it are only the table's words
        for k in range(count):
            at = tflat + 2 * k
            if at + 1 >= self.size or (k and at // BANK != tflat // BANK):
                break
            word = self.data[at] | self.data[at + 1] << 8
            if word == 0:
                if k == 0 or bound:
                    continue  # a table's first entry is often 0: no handler for state 0
                break
            if not bound and len(out) >= 4 and lowest is not None and table < word < lowest:
                break  # into the table's own bytes, or what follows them: past the table's end
            cflat = resolve(word, from_flat, from_cpu, s1, s2, self.size)
            if cflat is None:
                if bound:
                    continue
                break
            if not plausible(self.data, cflat, word):
                if bound or capped:
                    continue
                break
            if word > table:
                lowest = word if lowest is None else min(lowest, word)
            out.setdefault(cflat, (word, "table"))
        return out

    def default_base(self, bank: int) -> int | None:
        """Where a bank no code was followed into most likely runs: a game that only ever picks slot 2's bank keeps
        bank 1 in slot 1, so its other banks run from slot 2 (0x8000). None: no telling (the power-on mapping)."""
        w = self.found.slot_writes
        if bank >= 2 and self.mapper and w.get(2) and not w.get(1):
            return 0x8000
        return None

    def _bank_bases(self) -> None:
        """The CPU address each bank's code was found running at: the slot most of its functions run from."""
        votes: dict[int, dict[int, int]] = {}
        for flat, (cpu, _why) in self.found.starts.items():
            if cpu >= 0x4000:
                v = votes.setdefault(flat // BANK, {})
                v[cpu & ~(BANK - 1)] = v.get(cpu & ~(BANK - 1), 0) + 1
        self.found.bank_base = {b: max(v, key=v.get) for b, v in votes.items()}


_SYM = re.compile(r"^\s*([0-9A-Fa-f]{1,4}):([0-9A-Fa-f]{4})\s+([A-Za-z_.@$][\w.@$]*)\s*$")


def read_sym(text: str, seeds: bool = False) -> dict[tuple[int, int], str]:
    """A WLA-DX symbol file's labels: {(bank, CPU address): name}, from its [labels] section (or a file of bare
    `bank:address name` lines). Local labels (starting with `_` or `@`, or holding a dot) are left out. `seeds`:
    the automatic names emulators and disassemblers give code (`_LABEL_9D9_`) are kept too: where code starts, but
    no name for it."""
    out: dict[tuple[int, int], str] = {}
    section = "labels"
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "labels" or not (m := _SYM.match(line)):
            continue
        name = m.group(3)
        if (name.startswith(("_", "@")) and not (seeds and name.startswith("_LABEL_"))) or "." in name or "$" in name:
            continue
        out[(int(m.group(1), 16), int(m.group(2), 16))] = name
    return out


def sym_seeds(labels: dict[tuple[int, int], str], data: bytes) -> dict[int, tuple[int, str]]:
    """Seeds from a symbol file's labels that point at code in the ROM: {flat: (cpu, "sym")}."""
    out = {}
    for (bank, cpu), _name in labels.items():
        if cpu >= 0xC000:
            continue
        flat = cpu if cpu < 0x4000 else bank * BANK + (cpu & (BANK - 1))
        if flat < len(data) and plausible(data, flat, cpu):
            out[flat] = (cpu, "sym")
    return out
