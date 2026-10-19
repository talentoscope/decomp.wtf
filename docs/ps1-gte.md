# PS1 GTE matching from C

Pinned PS1 compiler profiles now supply a trusted GTE header before the shared context. Candidates call C macros
such as `gte_ldv0`, `gte_rtps` and `gte_stsxy`; direct inline assembly remains rejected. The helper header is
copied from the installed package after project includes are copied, so an include with the same filename cannot
replace it. Source and context edits cannot redefine the trusted macro names. Shared contexts retain their
existing size allowance and are checked for assembly as well as include-path escapes.

New PS1 contexts include basic vector, matrix, color and rectangle declarations. The agent's PS1 guide describes
the command, register and memory helpers. Function briefs report GTE instruction counts.

The command-name/opcode table is derived from [Rabbitizer's GTE macros](https://github.com/Decompollaborate/rabbitizer/blob/724a49a5b4dbfb99f1a9e6992e63964fd29c90c8/docs/r3000gte/gte_macros.h).
The header retains the MIT copyright and permission notice. No Sony SDK header is bundled. Parameterized command
fields and register indices are masked to their hardware fields; no macro accepts an arbitrary instruction word.

The memory helpers evaluate each pointer argument once and use memory clobbers. These constraints can change
code generation relative to another SDK macro implementation. The default lowercase command macros include two
preceding nops; uppercase forms distinguish padded (`gte_nRTPS`) and command-only (`gte_RTPS`) variants. Register
reads include a nop, and the assembler can schedule other work around transfers. Compare the actual linked bytes.
See [PSX-SPX](https://psx-spx.consoledev.net/geometrytransformationenginegte/) for command encodings and load delays.

PS1 COP2 code is eligible when the compiler profile supplies these helpers and the other function checks pass.
COP0, unsupported COP2 encodings, run-on functions and reads of uninitialized temporary registers retain their
existing exclusions. PS2 COP2 behavior is unchanged. Existing PS1 exclusions based solely on GTE use are reopened
when they pass the remaining checks; recognized libraries and human review marks stay set aside. A custom profile
can set `gte = false` to keep its previous header setup and COP2 exclusion.

Exports retain the trusted header and include it from each PS1 context header. Both bundled-assembler and maspsx
builds, normal attempts and donated workers use the same helpers. Normal header inclusion is used rather than
GCC's `-include` option because the pinned GCC 2.5.7 preprocessor crashes on that option in the validation fixture.

Validation uses synthetic source and the project's own demo executable. Tests cover all nine PS1 compilers with
both assembler paths, command words and padding, argument masks, single evaluation of pointer arguments,
source/context rejection, legacy exclusion migration, and a matched GTE function surviving a byte-identical export.
