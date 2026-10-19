"""Branch copies preserve distinct table facts and keep path-local writes independent."""
from decompnet.rom import sms_trace
from decompnet.rom.sms_state import State


def test_branch_copy_preserves_facts_and_isolates_mutable_state():
    assert sms_trace.State is State  # existing trace callers keep the same import
    original = State(1, 5, a=3, hl=0x500, s0=4, de=2, bc=1,
                     mem={0xC100: 5}, stack=[7], n=12, cmp=(3, 3), low=(4, 1),
                     bound=(5, 3, True), index=(6, 3, True), base=(7, 0x500))
    branch = original.copy()
    assert branch == original
    branch.mem[0xC100] = 6
    branch.stack.append(8)
    branch.s2 = 6
    assert original.mem == {0xC100: 5} and original.stack == [7] and original.s2 == 5


def test_branch_mutations_do_not_leak_into_a_sibling():
    original = State(1, 2, mem={0xC100: 3}, stack=[4])
    first, second = original.copy(), original.copy()
    first.mem.clear()
    first.stack.pop()
    assert second.mem == original.mem and second.stack == original.stack
