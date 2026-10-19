"""The source gate: register pins are declarations, not inline assembly.

A pin (`register u32 t __asm__("$4");`) only constrains which register a variable lives in; every
instruction still comes from the compiler, and it is the documented technique for register-allocation
hard tails (BFM-decomp matching cookbook, section 10). __asm__ *statements* stay forbidden, and the
pin pattern must not be usable to smuggle one in.
"""
import pytest

from decompnet.compiler import SourceRejected, check_source


@pytest.mark.parametrize("source", [
    'void f(void){ register u32 t __asm__("$4"); t = 1; }',
    'void f(void){ register u32 t asm("$4"); t = 1; }',
    'void f(void){ register u32 t __asm__ ("$a0") ; t = 1; }',
    'void f(void){ register u8 *q __asm__("$4"); q = 0; }',
    'void f(void){ register volatile u32 t __asm__("$4"); t = 1; }',
])
def test_register_pins_are_allowed(source):
    check_source(source)  # must not raise


@pytest.mark.parametrize("source", [
    'void f(void){ __asm__ __volatile__("" : : "r"(x)); }',
    'void f(void){ asm("nop"); }',
    'void f(void){ register u32 t __asm__("$4"); __asm__("break"); }',
    'void f(void){ register u32 t asm("$4"), u; u = 1; __asm__("break"); }',
    'void f(void){ register u32 t __asm__("$4; nop"); }',
    'void f(void){ register u32 t __asm__("$4"); } __asm__("nop");',
])
def test_asm_statements_and_smuggling_are_rejected(source):
    with pytest.raises(SourceRejected):
        check_source(source)


def test_other_forbidden_patterns_still_rejected():
    for source in ('void f(void) __attribute__((naked));',
                   'GLOBAL_ASM("x.s")',
                   '#pragma pack(1)',
                   'void f(void){ __asm__("nop"); }'):
        with pytest.raises(SourceRejected):
            check_source(source)


def test_sdccs_naked_is_rejected():
    """SDCC's `__naked` leaves out the return: `void f(void) __naked { x = 0; }` compiles to the store alone, which
    matched pieces of hand-written routines (an unrolled copy's ldi run) that no C function is."""
    with pytest.raises(SourceRejected, match="__naked"):
        check_source("void f(void) __naked { x = 0; }")
    with pytest.raises(SourceRejected, match="__naked"):
        check_source("u8 g(void) __naked\n{ return 4; }")
    check_source("void f(void) { /* __naked */ x = 0; } int not_naked; int __naked_count;")  # comments, other names


def test_ordinary_source_unaffected():
    check_source('void f(void){ u32 t = 1; t += 2; *(u8 *)0x1F8003D3 = t; }')
