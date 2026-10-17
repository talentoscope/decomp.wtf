# Getting C out of C64 assembly: what is possible, and a proposal

## Summary

The goal of decomp.wtf is readable C for code that exists only as machine code. Matching is not the goal: it is the *proof* that the agent's C is faithful. On the other systems the proof is cheap: the game's original C was built by a known compiler, so compiling the right C gives the original bytes back, and a byte match means the C is correct.

For most C64 games that proof is unavailable:

- Almost all commercial C64 games (Spy vs Spy included) were hand-written 6510 assembly. There is no original C, so no compiler reproduces the bytes. Readable C for them can still be written, but "the bytes match" can't be used to show it is right.
- cc65 is the only realistic C compiler to target. Its output has a distinctive shape: a software parameter stack at `$02/$03`, calls to runtime helpers (`pushax`, `tosaddax`, `incsp2`, ...), and 16-bit promotion. Hand-written code looks nothing like it (`func_851C`: 11 target bytes against about 60 bytes from cc65).
- There is no `m2c` for 6502, so agents start from the raw assembly with no draft.

**Conclusion:** the work is to replace byte-matching with a different proof of correctness, and to give agents a draft:

1. **Proof by behaviour (the core).** Run the original bytes and the candidate C in an emulator on many inputs. If registers, flags, memory writes and calls all agree, the C is functionally the same as the assembly. This gives a new status (`equivalent`), distinct from a byte `matched`.
2. **A 6502-to-C lifter**, so agents begin from a draft like on the other systems.
3. **Byte-exact matching** only for programs that were actually built with cc65 (detectable from the runtime), where it works as on the other systems.

## What the repo does today

- `6502-asm` (built in): function-by-function match of assembly, exact bytes. This is reliable.
- `cc65`: the agent writes C, which is compiled with `cc65 -t c64 -O`, assembled and linked, then compared byte for byte. In cc65 projects, plain assembly is now rejected (`engine.py`, `_compile_function`).
- Observed result on Spy vs Spy: only trivial functions match (an empty function, a jump-through stub). Everything else is hundreds to thousands of score points away.

## Proposal

The tiers are independent. Each is useful on its own, and later tiers build on earlier ones.

### Tier 0: set expectations (small)

- Label the compiler choices in the UI and prompts: "6510 assembly (recommended)" and "cc65 C (only for programs built with cc65)".
- Add a short note on the import and Settings screens explaining why, and link to this document.
- Keep the assembly rejection for cc65 projects. Decide separately whether to also reject `__asm__` / `asm()` blocks, since those are assembly in disguise.

### Tier 1: detect cc65-built programs (small to medium)

Goal: tell the user whether C matching can work for a given program before they spend effort.

- Fingerprint the cc65 runtime: scan the PRG for known runtime routines (`pushax`, `popax`, `tosaddax`, `incsp2`, `decsp2`, `shlax`, `staxspidx`, `ldaxidx`, and the `zerobss` / `copydata` startup code) by byte pattern.
- Fingerprint the startup (`c64.o`, `crt0`), which also reveals the memory configuration.
- Report "built with cc65 (likely)", with the matched routines listed, or "hand-written assembly (no cc65 runtime found)".
- Use the result to default the compiler choice: cc65 only if detected.
- Files: a new `src/decompnet/rom/c64_cc65.py` (signatures and a detector), called from `c64.read_program` / `_store_c64`.
- Optional extension: detect the cc65 version from differences in the runtime routines. This gives the exact version to install. The managed toolchain would need to support pinning several snapshots.

### Tier 2: a 6502 lifter that produces draft C (large, the main work)

This is the missing piece compared with `m2c`. For cc65-built code, the draft would get close to a match. For hand-written code, it gives readable, equivalent C.

Design:

1. **Lift to an IR.** Model A, X, Y, SP and the flags (C, Z, N, V) as variables, and memory as accesses to `*(volatile unsigned char *)addr`. Each instruction becomes a small statement; flag updates are tracked lazily so unused flags disappear.
2. **Structure the control flow.** Build the CFG from the existing `split()` output (basic blocks, branch targets), then recover `if` / `while` / `for` where the graph is reducible, and `goto` otherwise.
3. **Clean up.** Propagate constants, fold `lda/sta` pairs into assignments, and merge `clc; adc` into `+`.
4. **Recognise cc65 idioms** (needed for exact matches): the call and parameter-stack conventions, helper calls such as `pushax` / `tosaddax`, and 16-bit pointer arithmetic. Turn those into real C expressions again.
5. **Calling conventions.** For hand-written code, infer which registers a function reads before writing (inputs) and which it leaves live on return (outputs). Emit those as `register`-style parameters or as globals (`extern unsigned char reg_a;`).
6. **Integration.** Plug the lifter in where `seeds.generate_from_asm` is used. `_seed_for` and `_autopilot_one` currently return nothing for C64 projects.

Options for the implementation:

- Write the lifter in Python. It is the most controllable option, and the 6502 has only 56 documented opcodes, so the surface is small.
- Use Ghidra's 6502 processor module and its decompiler. Ghidra is already an optional tool in this repo. The cost is the 540 MB download and Java, and the output is generally poor for hand-written 6502 code.

I'd write our own: the instruction set is small, and the output can be shaped for the way agents and the matcher work.

### Tier 3: a second kind of "match": functional equivalence (medium)

For hand-written games, byte-exact C is impossible, but readable C that behaves the same is useful. This is a new verification mode and has to be presented separately from "MATCH".

- Compile the candidate C with cc65 (already supported).
- Run the target bytes and the candidate bytes in an emulator. `py65` is already a dependency, and its `MPU` can run a function from a given address until it returns.
- Seed both with the same random registers, flags and memory, run them, and compare the results: registers, flags and every written memory location. Do this for many random cases, plus edge values (0, `$FF`, `$80`, carry set and clear).
- Calls to other functions are stubbed: they record the call and a snapshot, so the same calls must happen in the same order.
- Hardware registers (`$D000-$DFFF`) are treated as write logs, compared in order.
- If all trials agree, mark the function as `equivalent` (a new status, distinct from `matched`). It is not byte-exact, so it counts differently in the progress numbers.
- Files: a new `src/decompnet/rom/c64_equiv.py` (harness), plus status and UI work in `engine.py`, `scoring.py` and `app.js`.

Caveats to state in the UI: random testing is not a proof. Functions that depend on timing, raster position, self-modifying code or undocumented opcodes need to be flagged and excluded.

### Tier 4: output as the end product (medium)

- For `matched` functions, the project keeps the assembly (always rebuildable, exact).
- For `equivalent` functions, the project keeps both the C (readable) and the original bytes. The rebuild uses the original bytes, so the result is always byte-identical, and the C is documentation and a base for a port.
- This also unlocks the C64 export (`exportable` is currently false): emit a `ca65` / `ld65` source tree with the assembly, the C where it exists, and a linker config that places everything at its original addresses.

## What I would do first

1. Tier 3 first (the equivalence checker): it is the proof that makes readable C acceptable for hand-written games, and agents can use it straight away with no lifter.
2. Tier 0 (labels and wording) alongside it.
3. Tier 2 (the lifter) next, starting with straight-line code and simple loops, measured on Spy vs Spy.
4. Tier 1 (cc65 detection) and byte-exact matching for programs that really were built with cc65.

## Test plan

- A small set of programs built with cc65 (a few homebrew sources compiled with known flags). For those, Tiers 1 and 2 should reach exact matches on many functions.
- The Spy vs Spy PRGs in `E:\DEV\retro\c64\prg` as the hand-written case, for Tiers 0, 3 and 4.
- Unit tests for the lifter: round-trip small known routines, and check the CFG and structuring on branches, loops and tail jumps.
- Unit tests for the equivalence harness: identical code, one flipped flag, a different store address, and a missing call must all be caught.

## Risks and open questions

- cc65 versions differ in their output, and the managed toolchain pins a single moving SourceForge snapshot. Exact matching of an older build needs an older cc65.
- The lifter's quality decides how much help agents get. It is the biggest piece of work.
- Equivalence testing says nothing about timing, and C64 code often depends on cycle counts (raster effects, loaders). Those functions should be excluded or marked.
- "Equivalent" must not be reported as "matched" in progress numbers or in the export.
- Linux and macOS: cc65 is installed automatically only on Windows today. The snapshot builds exist for other platforms from other sources, but I have not verified them.
- This is a proposal, and I have not built any of it. The estimates are rough.
