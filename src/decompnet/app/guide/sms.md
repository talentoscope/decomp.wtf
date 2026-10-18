# Matching Sega Master System and Game Gear code

Ported from SynapticDreams by Sam Wolfs (Copyright (c) 2026, MIT License; see `licenses/synaptic-sms-MIT.txt`).

The Master System (SMS) and Game Gear (GG) run a **Zilog Z80**. capstone has no Z80 backend, so the fork
carries its own disassembler (`decompnet.rom.z80`), checked against SDCC's assembler on every instruction
encoding. Both consoles use the same mapper and the same compiler, so everything here covers both.

## What the importer does with a .sms / .gg file

- A Sega header is recognised by content: `TMR SEGA` at 0x7FF0 (0x3FF0 or 0x1FF0 in a ROM of 16 or 8 KiB).
  Its last byte gives the region (Master System or Game Gear) and the size. A ROM with no header is still
  taken when it is named `.sms` or `.gg` and has a plausible size. A 512-byte copier header is dropped.
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
- Split-boundary correction is limited to `split_function`, which can divide one candidate when it clearly
  contains two routines. There is no `correct_split`, merge operation, or emulator trace/coverage import
  workflow in the app; report uncertain boundaries in a note rather than relying on those interfaces.
- The importer does not identify the game's original compiler or determine whether code was written in C
  or assembly. A hand-written routine may simply fail to match the available C matcher.

## Assembly export

Most Master System and Game Gear games contain hand-written assembly. The app does not provide an in-app
assembly-editing or assembly-matching track. Export creates a WLA-DX project from the user's ROM and current
split; the unmodified export is checked to rebuild the game byte for byte. Use it as a starting point for
external assembly work, not as an in-app routine-documentation workflow.

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

The split's boundaries can be wrong. If one candidate clearly contains two routines, use
`split_function(function, at=<CPU address where the second starts>)` to cut it. The app has no merge operation;
if a candidate appears to be only part of a routine, record the evidence in a note and release it rather than
assuming the boundary can be merged in-app.

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

- Keep the split's `func_<bank>_<address>` names unless a function's purpose is clear.
- Helper types and prototypes that several functions share go in the project's shared header
  (`get_context`), never edited per function to force a match.
