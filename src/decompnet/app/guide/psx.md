# Matching PlayStation (PS1) code

CPU: MIPS R3000A (MIPS I). Things that look odd at first:
- Load delay slot: the instruction after a load can't use the loaded value; you'll see `nop`s or
  reordering there. Branch delay slots exist too; there's no branch-likely.
- `mult`/`div` put results in HI/LO (`mflo`, `mfhi`). Division is followed by checks for divide by zero
  (`break 7`) and overflow (`break 6`): the PSY-Q assembler adds them. The bundled GNU assembler also
  emits these checks. Projects can optionally select maspsx emulation of a specific ASPSX version;
  the function brief identifies it when enabled. Write a plain `/`.
- `$gp`-relative accesses are one instruction instead of a full-address pair. The brief shows the
  **effective unit's** `-G` threshold and the project supplies a recovered/header GP when available.
  Thresholds can differ between resident code and overlays; a known load address does not establish GP.
  For a compiler-selected small common object, a definition such as `s32 counter;` may be needed in context
  where `extern s32 counter;` emits a full address. Verify the relocation and resulting bytes. Inferred
  symbol extents and non-GP references do not prove the original threshold; explicit sections, external
  declarations and code generation can change placement.

Start with the compiler, flags and assembler printed in the function brief. Settings supports per-unit
profiles across the pinned PS1 GCC variants (2.5.7 through 2.95.2) and optional maspsx/ASPSX emulation.
The bounded compiler comparison uses the same small drafts across recipes, reports ties/failures and
never changes settings automatically. A higher score is evidence from those samples, not proof of a
whole program's build recipe. Library objects may have a different recipe from game code.

PSY-Q libraries are recognized only when the user supplies compatible signature JSON. Recognition
records SDK candidates and sets eligible library routines aside; names do **not** establish prototypes,
and no SDK type/signature database is bundled. Exact BIOS dispatch wrappers receive gate/slot names.
Unknown BIOS slots stay numeric; runtime BIOS table patches can alter their meaning. Review the called
function's declaration before using its arguments or return value. Debug-file imports also offer names,
source basenames and declaration proposals; edit shared context through its normal regression checks.

GTE (the geometry coprocessor): pinned PS1 profiles supply trusted `gte_*` macros automatically. New ROM
contexts also have `SVECTOR`, `VECTOR`, `DVECTOR`, `CVECTOR`, `RECT` and `MATRIX` declarations.

- `gte_ldv0(p)`, `gte_ldv1(p)`, `gte_ldv2(p)` load vectors; `gte_ldv3(p0,p1,p2)` loads three.
- `gte_rtps()`, `gte_rtpt()`, `gte_nclip()` and other lowercase command helpers include two preceding nops.
  Rabbitizer-style `gte_nRTPS()` has the same padding; `gte_RTPS()` emits just the command.
- `gte_stsxy(p)`, `gte_stsxy3(p0,p1,p2)`, `gte_stsz(p)`, `gte_stotz(p)` and `gte_strgb(p)` store results.
- `gte_mfc2(reg)` / `gte_cfc2(reg)` read a register; `gte_mtc2(reg,value)` / `gte_ctc2(reg,value)` write it.
  Register indices are masked to 0..31. Reads include a nop for the CPU load delay.
- `gte_mvmva(sf,mx,v,cv,lm)` has bounded immediate fields; its arguments must be compile-time constants.
  `gte_sqr(sf)`, `gte_op(sf)`, `gte_gpf(sf)` and `gte_gpl(sf)` cover the other parameterized commands.

Memory helpers use memory clobbers, which can affect scheduling and register allocation. The selected assembler
also affects scheduling: compare the emitted bytes, including nops. m2c may not produce a draft for COP2 code;
use the assembly and these helpers. Source and context edits cannot contain raw assembly or redefine the
trusted macros. Recognized library routines stay set aside.

Encoding and timing reference: [PSX-SPX GTE](https://psx-spx.consoledev.net/geometrytransformationenginegte/).
The command-name/opcode table follows Rabbitizer's MIT-licensed GTE macros; its notice is retained in the header.

Typical patterns: `jtbl_` jump tables in .rodata for dense switches; overlays loaded at the same address
(separate units); `D_800xxxxx` data and `func_800xxxxx` names for things nobody named yet.

## Match the source to the observed idiom

- PS1 GCC defaults to unsigned plain `char` in the tested pinned variants. Compare `lb` with `lbu`, and
  `lh` with `lhu`; a mask or sign extension can come from an explicit cast, parameter conversion or a
  later use. Select `-fsigned-char` only when the unit's evidence supports it.
- An extra `sll`/`sra` pair can indicate a signed narrow parameter. Check the signature and all uses
  before changing it. K&R spelling is a bounded experiment: in the simple short-parameter example
  tested across all nine compilers, both spellings emitted the **same** conversion. It is not a general
  way to remove parameter extension. A wider parameter changes the declaration's meaning and needs
  caller/type evidence. Both permuters can try one type-preserving K&R spelling when a fresh compile
  shows extra entry-argument narrowing; ordinary compilation and linked verification still decide.
- Constant division can use a multiply-high sequence (`mult`/`multu`, `mfhi`, shifts and correction).
  Write the C division first; signed rounding and negative inputs make ad hoc shift rewrites unsafe.
- Structure assignment can become inline loads/stores or a copy loop. It is not guaranteed to call
  `memcpy`. If the target calls a copy helper, compare an explicit call, object size/alignment and the
  active flags. `-fbuiltin`/`-fno-builtin` can affect named calls; neither is a universal struct-copy fix.
- Aggregate returns can use a hidden destination pointer, shifting apparent arguments. In a measured
  GCC 2.7.2 two-word return example, adding `-fpcc-struct-return` changed no bytes. Examine the target
  convention before changing declarations or flags.
- Inlining, macro block structure, `register`, local lifetime and declaration order can change register
  allocation. Try one structural change at a time. Extra frames and merged tails are observations to
  investigate; they do not justify inserting raw assembly or empty assembly barriers.
- A surviving `andi ...,0xff` can require an explicit byte mask on a later use. A narrow variable type
  alone may let the optimizer remove it. Preserve the value's signedness and conversion semantics.
- For memory-mapped registers, use the access width and `volatile` behavior supported by the target.
  RAM, scratchpad and I/O addresses have different purposes; a constant in that range alone is not
  enough to identify a global or a hardware register.

## Calls, switches and layout

A missing prototype can make a draft infer the wrong call shape; an incorrect prototype can also add
conversions or shift arguments. Read argument registers, stack slots and callee evidence together. A
partial first block cannot prove a full signature, and a library name supplies no automatic ABI guarantee.

A `jtbl_` reference suggests switch data. Inspect the bound check, index scale and actual table words.
An indirect `jr` may instead be a callback or tail call; a plausible list of pointers is not a proven
jump table. Compare both code and referenced data.

Keep object alignment and padding separate from instruction mismatches. GCC may align jump-table or
constant sections more strongly than the final image. A function-level byte match does not establish
translation-unit data order, BSS placement or whole-disc layout. Exports retain individual source files. PS1 groups proposed from debug source basenames or an operator
request become shared source objects only when native offsets, padding, code/data and the complete rebuilt
image verify; rejected groups keep individual files. A basename alone does not prove an original translation unit.

Overlays sharing a runtime address remain separate units. Use the full function name and selected unit
for data reads and compiler settings. Disc import records discovered member offsets; opaque archive
code windows produce derived mapping candidates that need reviewed loader/trace evidence. Compressed
members and runtime captures need explicit provenance and boundaries. Do not infer a whole disc's LBA
layout from a rebuilt executable.

Measured compiler examples and scope:
[PS1 compiler observations](https://github.com/SynapticDreams/decomp.wtf/blob/main/docs/ps1-compiler-observations.md).
