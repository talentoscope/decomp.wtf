# Matching Sega Master System and Game Gear code

The Master System (SMS) and Game Gear (GG) run a **Zilog Z80**. capstone has no Z80 backend, so the fork
carries its own disassembler (`decompnet.rom.z80`), checked against SDCC's assembler on every instruction
encoding. Cartridge mapping and original compiler depend on the project; an SMS/GG extension does not establish them.

Choose the cartridge mapping and build intent in project Settings, with the evidence for that choice.
New imports and legacy projects with no declaration remain readable as provisional Sega-based listings;
accepting new C or documented assembly requires a supported declaration. Sega is currently supported under
the static-analysis limits below. Mapperless, Codemasters and other mappings can be recorded but remain
unsupported until their analysis models are integrated. A declaration does not establish hardware accuracy.
Profile changes retain existing work and refuse mapping changes that would reinterpret accepted source.
Experimental build intent is recorded separately from original reconstruction; whole-image equality still
compares against the frozen original input. The local operator API is `/api/sms/profile` (GET or POST).

Reference resolution distinguishes CPU addresses from cartridge offsets. An established code entry identifies
its own mapped window; its display placement does not establish banks in other windows. Only Sega's first KiB
is fixed to bank 0. Mapper-register reads see work RAM, including its mirror: writing the other RAM alias can
change that readback without selecting a new bank. Reset register values are a selected analysis recipe,
not a guarantee for every mapper revision. Full bus-context propagation and consumer migration are in progress;
the provisional trace still uses a cartridge-only reset model and cannot establish BIOS or cartridge-RAM execution.
Standalone exports include this shared resolver. Generated reference names require a declared model and established
instruction geometry; unresolved references remain numeric while the build still verifies whole-image equality.

## What the importer does with a .sms / .gg file

- A Sega header is recognised by content: `TMR SEGA` at 0x7FF0 (0x3FF0 or 0x1FF0 in a ROM of 16 or 8 KiB).
  Its last byte gives the region (Master System or Game Gear) and the size. A ROM with no header is still
  taken when it is named `.sms` or `.gg` and has a plausible size. A 512-byte copier header is dropped.
  The project retains SHA-256 identities for the supplied dump and normalized ROM, including any removed header.
  Reimporting the same revision with another wrapper adds input evidence. Export refuses changed comparison
  bytes before replacing existing output; headerless inputs keep product/revision unknown.
- The cartridge is split into **16 KiB banks**. Bank 0 is at 0x0000. Which bank is mapped where is decided by writes
  to 0xFFFE/0xFFFF at run time, so the code is followed with the banks it maps (`ld a,5 / ld ($ffff),a` puts bank
  5 at 0x8000): each function runs at the address its bank is mapped to when it's called, and its calls and jumps
  into its own bank read as the ROM has them. Most games keep bank 1 at 0x4000 and page the rest into 0x8000.
  Names spell the bank and the address the code runs at: `func_<bank>_<address>`.
- Functions are found from the reset vector, the RST handlers and the NMI by following calls, tail jumps, bank
  switchers and the tables dispatchers read (`ld hl,table / jp dispatch`, or a function's own
  `ld hl,table / add hl,de / ... / jp (hl)`, as many entries as a `cp n / jr nc` before it allows), plus a sweep for
  `call`s in the banks that hold code and code that starts right where a function ends. The following also keeps
  slot 0 paged by 0xFFFD, a bank kept in RAM or saved on the stack and mapped again, and a computed call
  (`ld de,.next / push de / jp (hl)`), which comes back to `.next`. A symbol file next to the ROM names functions,
  and its automatic `_LABEL_..._` names say where code starts. Code reached only through pointers kept in RAM may
  still be missing, and data inside a function's range is not told apart from code.
- When the split took data for code, missed a function, or decoded the bytes after a call as instructions, say so
  with `correct_split(kind, at, reason, count)`, with how you know (what reads it, what calls it, what it holds):
  `data` (`count` bytes at `at` aren't code), `code` (a function starts at `at`), `table` (`count` handler
  addresses at `at`), `operands` (the routine at `at` reads `count` bytes after each call to it and returns past
  them), `noreturn` (it never returns). `at` is a function's name, `NN:AAAA` (the bank and the address it runs at,
  `05:8123` for bank 5 in slot 2) or an address in `function`'s bank. It's used the next time a person splits the
  game again (they can undo it first); ten an hour at most.
- A person can also import what an emulator saw while they played (Settings, a Mesen2 code/data log or a
  `bank:address` trace): splitting the game again then starts functions where calls entered and where code ran that
  wasn't reached, and keeps what was only read as data out of functions.

- The import reads which toolchain built the game: GBDK-2020's start-up code (C), or an SDSC tag (homebrew,
  not which tool). A game none of them built is most likely assembly, for the assembly track.

## Hand-written routines

Most Master System and Game Gear games are assembly. A routine set aside as hand-written is documented instead of
matched: WLA-DX assembly that rebuilds its bytes, with names, comments and constants (`claim_function(track="asm")`,
guide `asm`). The project's export is a WLA-DX disassembly made from the user's copy, with each documented routine
in place of its bytes, checked to rebuild the game byte for byte.

## The matching lane

Matching uses the pinned **GBDK-2020** toolchain: `sdcc -mz80` for C, `sdasz80` for assembly, the same
archive as the Game Boy. The app installs it when a game is imported. The ROM's call and jump addresses are absolute, so the
build pins every symbol to the ROM's own address and compares bytes directly, with nothing masked.

Until the toolchain is installed a `.sms`/`.gg` project is imported **read-only**: disassembled and
searchable, but not handed out for matching, because no candidate could be judged.

## What's set aside

Most Master System and Game Gear games are hand-written assembly. The app sets aside what its split hands out that no C function is (status *assembly* or *not code*,
with the reason): pieces of a routine that run on into the next function (the split starts a function wherever code
jumps or branches to, and hand-written code branches into the middle of its routines), bytes that are no
instruction, `halt` (inline assembly only), padding, and a stretch the split ran its longest on. If a function you're given looks like one of
those, say so in a note and release it. `__naked` is refused: it leaves out the function's return, so it can only
reproduce such a piece, and a match has to be a whole function from C.

The split's boundaries can be wrong both ways. When a function you're given is two routines (one ends, the next
starts), `split_function(function, at=<CPU address where the second starts>)` cuts it. When it's half of one (it runs
on into the next function or jumps into it, or the one before runs on into it, and nothing else calls the second
half), `merge_function(function)` joins them. Each piece is checked again for what's set aside.

## What SDCC does on the Z80 (`sdcccall(1)`)

- **Arguments:** the first byte argument is in A and the second in L. 16-bit arguments are in HL, then DE.
  A 16-bit argument that follows a byte one is in DE. Further arguments go on the stack, and the callee
  pops them (`pop hl … jp (hl)` is how SDCC returns from such a function).
- **Results:** a byte comes back in A, a 16-bit value in **DE** (`add hl, de` then `ex de, hl`).
- **Ports:** declare them as `__sfr __at(0xBF) VDP_CTRL;`. Writing one is `out ($bf), a`, reading is
  `in a, ($be)`. The VDP is at ports 0xBE (data) and 0xBF (control), the controllers are read at 0xDC and 0xDD.
- **Memory:** a fixed RAM address is `*(volatile u8 *)0xC000`, which is `ld ($c000), a` / `ld a, ($c000)`.
  RAM is 0xC000-0xDFFF (mirrored at 0xE000); the mapper registers are 0xFFFC-0xFFFF.
- **`rst` calls:** `void func_000_0028(void) __z88dk_shortcall(0x28, 0);` then `func_000_0028();` builds to `rst $28` followed by
  the byte `0x00` (the second number is the byte that follows). Declare the function with its own name, as the project knows it.
- **Calls:** a call in tail position becomes `jp`. If the target has `call f` then `ret`, give the call
  something to do afterwards (a statement after it, or return a value) to get the `call`.

## What moves SDCC's output

The lane is new, so this list is short on purpose. Add what you measure with `add_note` (no function
name) so the next session starts ahead:

- Local declaration order and first use change register allocation: reorder before rewriting.
- Narrow types matter. `u8`/`s8` locals and `int` truncate differently, and `int` is 16 bits here.
- New projects build with `--fsigned-char` and no optimization flag, which is what GBDK's own `lcc` does. A game built
  another way may need `--opt-code-size` or `--opt-code-speed` (the project's compiler flags): if a loop or a jump comes
  out as `jp` where the target has `jr`, try those before rewriting, and say what worked in a note.

## Conventions

The synthetic assembly and export checks run in the **SMS assembly** CI workflow with the pinned WLA-DX.
To run that gate locally, install `decompnet toolchain install wla-dx`, then run
`uv run python tools/sms_gate.py` from the repository. It checks SMS and GG exports and fails on missing
tools, failed comparisons or skipped tests. The default report is `build/sms-tool-tests.xml`.

- Keep the split's `func_<bank>_<address>` names unless a function's purpose is clear.
- Helper types and prototypes that several functions share go in the project's shared header
  (`get_context`), never edited per function to force a match.
