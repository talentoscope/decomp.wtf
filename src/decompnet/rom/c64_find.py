"""Commodore 64 function finding: static analysis of one 64 KB RAM image, in layers.

1. certain: follow calls and jumps from every real entry point. Those are the program's entry (a BASIC SYS
   target or where the CPU was), the IRQ/NMI vectors that are live for the processor port ($01) when the RAM was
   taken, and return addresses still on the stack.
2. certain: pointers the code builds itself, followed in rounds. These are IRQ/NMI handler installs (#lo/#hi
   stored into a vector), jump tables reached through ``jmp (ptr)``, pha/pha/rts tables and pushed address + rts,
   and self-modified ``jmp`` operands.
3. probable: every byte no code claims yet is tried as the start of a routine and scored. It scores higher for
   ending in rts/rti, touching hardware, calling the KERNAL or a certain function, being an address the code
   builds, and appearing as a word elsewhere. It scores lower for branching into the middle of known
   instructions, calling outside RAM, or being mostly $00/$FF. A start right after other code ends (an rts, rti or
   jmp) scores higher too: code usually follows code. And a start that falls inside an instruction of the bytes
   read from just after that code, when those bytes read as a routine too, moves back to where they start (a word
   elsewhere that happens to hold its address is the usual reason the wrong one scored higher). This layer is
   deliberately generous: a decompiling agent can mark a probable function as not real code.
4. coverage: how many bytes are certain code, probable code, BASIC, or still unexplained.

The BASIC program is left out of every layer: it is BASIC's, and only the BASIC decoder reads it.
Nothing is run: this never executes the program.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import c64_hw, mos6502

RAM = 0x10000
MAX_FUNCTION = 0x1000
MIN_SCORE = 5
VECTORS = {0xFFFE: "IRQ", 0xFFFA: "NMI", 0xFFFC: "RESET", 0x0314: "CINV", 0x0316: "CBINV", 0x0318: "NMINV",
           0x0328: "ISTOP"}
_MEMORY = ("abs", "abx", "aby", "zp", "zpx", "zpy")


@dataclass
class Found:
    """One function the finder believes in: where it starts, how sure it is and why."""

    start: int
    confidence: str                      # "certain" or "probable"
    why: set[str] = field(default_factory=set)
    insns: dict[int, int] = field(default_factory=dict)   # instruction address -> length, along its flow
    targets: set[int] = field(default_factory=set)        # branch and jump targets inside it
    score: int | None = None                              # probable functions only

    def reason(self) -> str:
        return sorted(self.why)[0] if self.why else ""


@dataclass
class Result:
    functions: list[Found]
    coverage: dict
    basic: tuple[int, int] | None


class Finder:
    def __init__(self, ram: bytes, p01: int | None = None, basic: tuple[int, int] | None = None):
        self.ram = bytes(ram)
        self.p01 = p01
        self.basic = basic
        self.funcs: dict[int, Found] = {}
        self.code: dict[int, str] = {}          # address -> "C" certain / "P" probable, for every code byte
        self.starts: set[int] = set()           # instruction starts
        self.ptr_pairs: dict[int, set[int]] = {}  # ptr -> addresses built from #lo/#hi stored to ptr/ptr+1
        self.work: list[tuple[int, str, str]] = []
        #: while probable code is followed: what it calls, held back to be scored like any other probable start
        self.held: list[tuple[int, str]] | None = None
        self.rom = c64_hw.rom_by_addr()

    # ------------------------------------------------------------ memory map

    def rom_mapped(self, a: int) -> bool:
        """BASIC or KERNAL ROM is what the CPU sees at ``a`` (unknown port: assume the default, both in)."""
        port = 0x37 if self.p01 is None else self.p01
        if 0xE000 <= a <= 0xFFFF:
            return bool(port & 2)
        if 0xA000 <= a <= 0xBFFF:
            return port & 3 == 3
        return False

    def io_mapped(self, a: int) -> bool:
        port = 0x37 if self.p01 is None else self.p01
        return 0xD000 <= a <= 0xDFFF and bool(port & 3) and bool(port & 4)

    def in_basic(self, a: int) -> bool:
        return self.basic is not None and self.basic[0] <= a < self.basic[1]

    def runs(self, a: int) -> bool:
        """The program's own RAM: not ROM, not I/O, not the BASIC program."""
        return 0 <= a < RAM and not self.rom_mapped(a) and not self.io_mapped(a) and not self.in_basic(a)

    def dec(self, pc: int):
        if not self.runs(pc):
            return None
        op = self.ram[pc]
        known = mos6502.OPCODES.get(op)
        if known is None:
            return None
        name, mode = known
        n = mos6502._MODES[mode]
        if pc + n > RAM or any(not self.runs(pc + i) for i in range(1, n)):
            return None
        v = self.ram[pc + 1] if n == 2 else (self.ram[pc + 1] | self.ram[pc + 2] << 8) if n == 3 else None
        if mode == "rel":
            v = (pc + 2 + (v - 256 if v > 127 else v)) & 0xFFFF
        return name, mode, n, v

    def table(self, lo: int, hi: int, adj: int = 0, limit: int = 64) -> list[int]:
        """Entries of a word table (hi at lo+1: interleaved) or split lo/hi byte tables."""
        stride = 2 if hi in (lo, lo + 1) else 1
        if hi == lo:
            hi = lo + 1
        out = []
        for i in range(limit):
            a, b = lo + i * stride, hi + i * stride
            if a >= RAM or b >= RAM:
                break
            t = ((self.ram[a] | self.ram[b] << 8) + adj) & 0xFFFF
            if t < 0x200 or not self.dec(t) or min(lo, hi) <= t < max(lo, hi) + limit * stride:
                break
            out.append(t)
        return out

    # ------------------------------------------------------------ layers 1 and 2

    def add(self, a: int, why: str, conf: str = "certain") -> None:
        if a is not None and self.dec(a) and self.ram[a] != 0x00:  # a brk is zeroed memory, not a routine
            if conf == "probable" and self.held is not None and not why.startswith("probable"):
                self.held.append((a, why))  # only probable code calls it: it earns its place with a score
                return
            self.work.append((a, why, conf))

    def run(self) -> None:
        while self.work:
            start, why, conf = self.work.pop()
            f = self.funcs.get(start)
            if f is not None:
                f.why.add(why)
                if conf == "certain" and f.confidence != "certain":
                    f.confidence, f.score = "certain", None
                    f.why = {w for w in f.why if not w.startswith("probable")}
                    self._flow(f)
                continue
            f = self.funcs[start] = Found(start, conf, {why})
            self._flow(f)

    def _flow(self, f: Found) -> None:
        tag = "C" if f.confidence == "certain" else "P"
        todo, seen = [(f.start, {}, {}, [])], set()
        f.insns.clear()
        while todo:
            pc, imm, tsrc, pushes = todo.pop()
            imm, tsrc, pushes, psrc, last = dict(imm), dict(tsrc), list(pushes), {}, {}
            while pc not in seen:
                d = self.dec(pc)
                if not d:
                    break
                seen.add(pc)
                name, mode, n, v = d
                self.starts.add(pc)
                f.insns[pc] = n
                for i in range(n):
                    if self.code.get(pc + i) != "C":
                        self.code[pc + i] = tag
                reg = {"lda": "a", "ldx": "x", "ldy": "y"}.get(name)
                if reg:
                    imm[reg] = v if mode == "imm" else None
                    tsrc[reg] = v if mode in ("abx", "aby") else None
                elif name in ("tax", "tay", "txa", "tya"):
                    imm[name[2]], tsrc[name[2]] = imm.get(name[1]), tsrc.get(name[1])
                elif name in ("pla", "inx", "iny", "dex", "dey", "adc", "sbc", "and", "ora", "eor", "asl", "lsr",
                              "rol", "ror"):
                    r = {"inx": "x", "iny": "y", "dex": "x", "dey": "y"}.get(name, "a")
                    if mode in ("imp", "acc", "imm") or name in ("adc", "sbc", "and", "ora", "eor"):
                        imm[r] = None
                        if name not in ("inx", "iny", "dex", "dey"):
                            tsrc[r] = None
                st = {"sta": "a", "stx": "x", "sty": "y"}.get(name)
                if st and mode in ("abs", "zp"):
                    psrc[v] = tsrc.get(st)
                    last[v] = imm.get(st)
                    if imm.get(st) is not None and last.get(v - 1) is not None:
                        self.ptr_pairs.setdefault(v - 1, set()).add(last[v - 1] | imm[st] << 8)
                    if imm.get(st) is not None and last.get(v + 1) is not None:  # hi stored first
                        self.ptr_pairs.setdefault(v, set()).add(imm[st] | last[v + 1] << 8)
                if name == "pha":
                    pushes.append((imm.get("a"), tsrc.get("a")))
                elif name == "pla" and pushes:
                    pushes.pop()
                if name == "jsr":
                    self.add(v, f"called from ${pc:04X}", f.confidence)
                elif name == "jmp" and mode == "abs":
                    if self.runs(v):
                        f.targets.add(v)
                        pc = v
                        continue
                    break
                elif name == "jmp":
                    lo, hi = psrc.get(v), psrc.get(v + 1)
                    if lo is not None and hi is not None:
                        for t in self.table(lo, hi):
                            self.add(t, f"jump table ${lo:04X} through jmp (${v:04X}) at ${pc:04X}", f.confidence)
                    break
                elif name == "rts":
                    if len(pushes) >= 2:
                        (hv, ht), (lv, lt) = pushes[-2], pushes[-1]
                        if hv is not None and lv is not None:
                            self.add(((lv | hv << 8) + 1) & 0xFFFF, f"pushed address + rts at ${pc:04X}", f.confidence)
                        elif ht is not None and lt is not None:
                            for t in self.table(lt, ht, adj=1):
                                self.add(t, f"rts table ${lt:04X} at ${pc:04X}", f.confidence)
                    break
                elif name in ("rti", "brk"):
                    break
                elif mode == "rel":
                    f.targets.add(v)
                    todo.append((v, imm, tsrc, pushes))
                pc += n

    def resolve_pointers(self) -> int:
        """#lo/#hi pairs stored into a CPU/KERNAL vector, or into a jmp operand (self-modifying code)."""
        new = 0
        for p, targets in list(self.ptr_pairs.items()):
            if p in VECTORS:
                kind = f"{VECTORS[p]} vector install"
            elif (p - 1) in self.starts and self.ram[p - 1] == 0x4C:
                kind = f"self-modified jmp at ${p - 1:04X}"
            else:
                continue
            for t in targets:
                if t not in self.funcs and self.dec(t):
                    self.add(t, kind)
                    new += 1
        self.run()
        return new

    # ------------------------------------------------------------ layer 3

    def probable(self, min_score: int = MIN_SCORE) -> int:
        """Starts in bytes no certain code reaches, kept when they score ``min_score``; what they call is scored the
        same way before it's kept (one bad guess doesn't pull in everything it seems to call)."""
        words: dict[int, int] = {}
        for i in range(RAM - 1):
            if not self.code.get(i) and not self.in_basic(i):
                w = self.ram[i] | self.ram[i + 1] << 8
                words[w] = words.get(w, 0) + 1
        built = set().union(*self.ptr_pairs.values()) if self.ptr_pairs else set()
        certain = {a for a, f in self.funcs.items() if f.confidence == "certain"}
        self._words, self._built, self._certain = words, built, certain
        cands = []
        for a in range(0x200, RAM):
            if self.code.get(a) or not self.runs(a):
                continue
            got = self._score(a)
            if got is None or got[0] < min_score:
                continue
            moved = ""
            if (g := self.aligned_start(a)) is not None and (head := self._score(g)) is not None:
                # inside an instruction of the bytes that follow the code before it, and those read as a routine:
                # that reading is the real one (the guess's evidence, say a word that holds its address, still counts)
                moved = f"; start moved back from ${a:04X}, which is inside an instruction of the code from ${g:04X}"
                a, got = g, (max(got[0], head[0]), head[1], head[2])
            cands.append((got[0], a, got[1], got[2], moved))
        cands.sort(key=lambda c: (-c[0], c[1]))
        taken: set[int] = set()
        accepted = []
        for score, a, size, end, moved in cands:
            span = set(range(a, a + size))
            if span & taken:
                continue
            taken |= span
            accepted.append((score, a, end, moved))
        scores = {a: score for score, a, _end, _moved in accepted}
        self.held = []
        for score, a, end, moved in accepted:
            self.add(a, f"probable (score {score}, ends {end}{moved})", "probable")
        for _round in range(50):
            self.run()
            waiting, self.held = self.held, []
            for a, why in dict(waiting).items():
                if a in self.funcs or self.code.get(a) or not self.runs(a):
                    continue
                got = self._score(a)
                if got is None or got[0] < min_score:
                    continue
                scores[a] = got[0]
                self.add(a, f"{why}, probable (score {got[0]}, ends {got[2]})", "probable")
            if not self.work:
                break
        self.held = None
        for a, score in scores.items():
            if a in self.funcs and self.funcs[a].confidence == "probable":
                self.funcs[a].score = score
        return len(scores)

    def reach_back(self, limit: int = 64) -> int:
        """A probable routine that branches back before its own start: the start was a guess, and the code it branches
        to is its head (the head scored low, say for calling a ROM that isn't mapped). When the bytes before it, from
        just after other code, run straight into that target, the routine starts there instead."""
        moved = 0
        for old, f in sorted(self.funcs.items()):
            if f.confidence != "probable" or not f.insns or (lo := min(f.insns)) >= old:
                continue
            first, a = None, lo - 1
            while a >= max(0x200, lo - limit) and not self.code.get(a) and self.runs(a):
                pc = a
                while pc < lo:
                    d = self.dec(pc)
                    if not d or d[0] in ("brk", "rts", "rti", "jmp") or any(self.code.get(pc + i) for i in range(d[2])):
                        break
                    pc += d[2]
                if pc == lo:
                    first = a
                a -= 1
            if first is None or first in self.funcs:
                continue
            del self.funcs[old]
            f.start = first
            f.why.add(f"probable start moved back from ${old:04X}: it branches back to ${lo:04X}, which "
                      f"${first:04X} runs into")
            self.funcs[first] = f
            self._flow(f)
            moved += 1
        return moved

    def after_code(self, a: int) -> bool:
        """Whether known code ends right before ``a`` with an rts, rti or jmp: the next byte is where code would go."""
        p = a - 1
        if p in self.starts and self.code.get(p) and self.ram[p] in (0x60, 0x40):  # rts, rti
            return True
        p = a - 3
        return p >= 0 and p in self.starts and bool(self.code.get(p)) and self.ram[p] in (0x4C, 0x6C)  # jmp

    def aligned_start(self, a: int, limit: int = 64) -> int | None:
        """Where the routine really starts when ``a`` is inside one of its instructions: the bytes from just after
        the code before ``a`` (an rts, rti or jmp, within ``limit`` bytes) read as instructions straight over ``a``,
        without stopping. None when ``a`` is on an instruction boundary of those bytes, or there's no such code."""
        g = a
        while g > 0x200 and a - g < limit and not self.code.get(g - 1) and self.runs(g - 1):
            g -= 1
        if g == a or not self.after_code(g):
            return None
        pc = g
        while pc < a:
            d = self.dec(pc)
            if not d or d[0] in ("brk", "rts", "rti", "jmp"):
                return None
            pc += d[2]
        return g if pc != a else None

    def _score(self, a: int) -> tuple[int, int, str] | None:
        """(score, size, how it ends) for a run of bytes at ``a`` read as a routine, or None when it doesn't read as one."""
        words, built, certain = self._words, self._built, self._certain
        pc, insns, hw, calls, ok, end, bad, zeros, size = a, 0, 0, 0, False, "", 0, 0, 0
        while insns < 300:
            if self.code.get(pc):
                ok, end = pc in self.starts, "falls into known code"
                break
            d = self.dec(pc)
            if not d or d[0] == "brk":
                break
            name, mode, n, v = d
            insns += 1
            size += n
            zeros += sum(1 for i in range(n) if self.ram[pc + i] in (0, 0xFF))
            if mode in _MEMORY and c64_hw.name_of(v):
                hw += 1
            if name == "jsr":
                if v in self.rom and self.rom_mapped(v) or v in certain:
                    calls += 1
                elif not self.runs(v):
                    bad += 1
            if mode == "rel" and self.code.get(v) and v not in self.starts:
                bad += 1
            if name in ("rts", "rti", "jmp"):
                ok, end = True, name
                break
            pc += n
        if not ok or insns < 3:
            return None
        score = (2 if end in ("rts", "rti") else 1) + min(hw, 3) + min(2 * calls, 4)
        score += 3 if a in built else 0
        score += min(words.get(a, 0), 2) + min(words.get(a - 1, 0), 1)
        score -= 3 * bad
        score -= 3 if zeros * 3 > size else 0
        score += 1 if insns >= 8 else 0
        score += 3 if self.after_code(a) else 0  # code usually follows code
        return score, size, end

    # ------------------------------------------------------------ layer 4

    def coverage(self) -> dict:
        certain = sum(1 for v in self.code.values() if v == "C")
        probable = sum(1 for v in self.code.values() if v == "P")
        basic = (self.basic[1] - self.basic[0]) if self.basic else 0
        gaps, a = [], 0x0200
        while a < RAM:
            if self.code.get(a) or not self.runs(a):
                a += 1
                continue
            e = a
            while e < RAM and not self.code.get(e) and self.runs(e):
                e += 1
            gaps.append((a, e - a))
            a = e
        biggest = sorted(gaps, key=lambda g: -g[1])[:16]
        return {"certain_bytes": certain, "probable_bytes": probable, "basic_bytes": basic,
                "unexplained_bytes": sum(n for _a, n in gaps),
                "certain_functions": sum(1 for f in self.funcs.values() if f.confidence == "certain"),
                "probable_functions": sum(1 for f in self.funcs.values() if f.confidence == "probable"),
                "biggest_unexplained": [[s, n] for s, n in sorted(biggest)]}


def find(ram: bytes, entry: int | None, *, p01: int | None = None, sp: int | None = None,
         basic: tuple[int, int] | None = None, sys_targets: list[int] = (), vectors: list[int] = (),
         min_score: int = MIN_SCORE) -> Result:
    """Every function in ``ram``. ``p01``: the processor port when the RAM was taken (None: unknown, so the
    default memory map and the vectors given in ``vectors``). ``sp``: the stack pointer then, for return
    addresses. ``basic``: the BASIC program's range, kept out. ``sys_targets``: its SYS addresses."""
    f = Finder(ram, p01, basic)
    if entry is not None:
        f.add(entry, "program entry")
    for t in sys_targets:
        f.add(t, f"SYS {t} in BASIC")
    if p01 is None:
        for t in vectors:
            f.add(t, "vector")
    elif p01 & 2:  # KERNAL mapped: its IRQ/NMI handlers go through the RAM vectors at $0314 / $0318
        for vec, nm in ((0x0314, "CINV (KERNAL IRQ)"), (0x0318, "NMINV (KERNAL NMI)")):
            f.add(f.ram[vec] | f.ram[vec + 1] << 8, f"{nm} vector")
    else:  # KERNAL banked out: the CPU's own vectors are RAM ($FFFC never: reset maps the KERNAL back in)
        for vec, nm in ((0xFFFA, "NMI"), (0xFFFE, "IRQ")):
            f.add(f.ram[vec] | f.ram[vec + 1] << 8, f"{nm} vector")
    if sp is not None:
        a = 0x101 + sp
        while a < 0x1FF:
            r = (f.ram[a] | f.ram[a + 1] << 8) + 1
            if 3 <= r < RAM and f.ram[r - 3] == 0x20 and f.runs(r):
                f.add(r, f"return address on the stack at ${a:04X}")
                a += 2
            else:
                a += 1
    f.run()
    for _round in range(10):
        if not f.resolve_pointers():
            break
    f.probable(min_score)
    f.reach_back()
    order = sorted(f.funcs.values(), key=lambda x: x.start)
    return Result(order, f.coverage(), basic)


def body(found: Found, others: set[int]) -> tuple[int, dict[int, int]]:
    """The contiguous bytes a function owns: from its start through its instructions, across small skipped
    stretches its own branches jump over, up to the next function's start. (end, {island: size})."""
    end, islands = found.start, {}
    for addr in sorted(a for a in found.insns if a >= found.start):
        if addr >= found.start + MAX_FUNCTION or (addr != found.start and addr in others):
            break
        if addr > end:
            if addr - end <= 16 and addr in found.targets:
                islands[end] = addr - end
            else:
                break
        end = max(end, addr + found.insns[addr])
    return min(end, found.start + MAX_FUNCTION), islands
