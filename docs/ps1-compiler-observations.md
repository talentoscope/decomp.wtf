# PS1 compiler observations

These are original microprograms compiled with this repository's pinned tools. They qualify matching advice; they do not establish the recipe or ABI of any validation project. The checks used `compile_source`, `extract_function` and the repository's disassembler with the native assembler, `-O2 -G0`, and an empty context. All nine installed PS1 GCC variants were checked for the first two rows. Other rows used `gcc-2.7.2-psx` only.

| Source/experiment | Observed output | Practical implication |
| --- | --- | --- |
| `char g; int f(void) { return g; }` | `lui; lbu; jr; nop` in all nine variants | Plain `char` is unsigned in these default recipes; explicit signedness can differ. |
| `int f(short x) { return x + 1; }` versus `int f(x) short x; { return x + 1; }` | Both: `sll; sra; jr; addiu`, byte-identical for each variant | K&R spelling alone did not remove the conversion in this example. |
| `int f(int x) { return x / 10; }` | Constant load, `mult`, `mfhi`, shifts/correction; no hardware `div` | Start with C division; the compiler supplies signed correction. |
| Copy a struct containing 3, 4 or 8 `int` elements with `*d = *s;` | Inline `lw`/`sw`, no copy-helper relocation | Struct assignment is not guaranteed to select `memcpy`. |
| Copy a struct containing 16 or 64 `int` elements | An inline load/store loop | Larger aggregate size alone did not select a helper in this recipe. |
| Copy a struct containing `char bytes[13]` | `lwl/lwr`, `lb`, `swl/swr`, `sb` | Alignment can select unaligned operations. Also observed with `-fno-builtin`, with `-fno-builtin -fno-inline`, and at `-O0`. |
| Return a two-`int` struct with both fields initialized, with/without `-fpcc-struct-return` | Byte-identical output | The extra flag did not change this example; inspect each target's return convention. |

Reproduce a copy with `struct Pair { int values[3]; }; void f(struct Pair *d, struct Pair *s) { *d = *s; }`, varying only the element count. Reproduce the return with `struct Pair { int a,b; }; struct Pair f(int x) { struct Pair p; p.a=x; p.b=x+1; return p; }`.

Compiler version, flags, assembler version, alignment, aliases and surrounding translation-unit declarations can change results. Different behavior on a real target should lead to a smaller reproducer and a comparison of its effective unit profile. The existing compiler and assembler tests cover signedness, small data, division guards and version-dependent constants; these additional manual observations document the limits of broader research claims. No retail source or image was used.
