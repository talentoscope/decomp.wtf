"""Path-local Z80 facts and discovered SMS/GG entries, independent of trace traversal.

A branch preserves every fact and owns its memory/stack containers. Unknown values stay unknown;
register semantics and mapper resolution belong to their respective analysis modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

ARG = "arg"  # A as the function was entered: a mapper helper can receive its caller's bank in A
FRESH = 16  # instructions within which a bound or table base belongs to the following dispatch


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
        return replace(self, mem=dict(self.mem), stack=list(self.stack))

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
