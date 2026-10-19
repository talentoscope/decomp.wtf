# Matching Game Boy and Game Boy Color code

Game Boy (DMG) and Game Boy Color (CGB) cartridges run a Sharp **SM83** (LR35902) — a 4.19 MHz
8080/Z80 relative with its own instruction set. capstone has no Game Boy backend, so the fork carries
its own disassembler (`decompnet.rom.sm83`) and reads cartridges through `mgbdis`-style output.

## What the importer does with a .gb / .gbc file

- Detection is by content, not extension: the Nintendo logo at 0x104, a matching header checksum at
  0x14D, and a power-of-two size of 16 KiB banks (32 KiB … 8 MiB).
- The header gives the title, game code, CGB flag, cartridge type, ROM/RAM size and region.
- Function discovery splits the cartridge into **one segment per 16 KiB bank**: bank 0 lives at
  0x0000–0x3FFF and every other bank is switched into the 0x4000–0x7FFF window, which is what the
  hardware does. Automatic names are bank-scoped, so a function in bank 3 keeps its address inside
  that bank.
- Before mgbdis disassembles it, the game's code is followed from its vectors with the bank it maps
  (`ld a,N / ld ($2000),a`), through its jump tables, its far calls (`ld a,BANK / ld hl,Func / rst $08`,
  or `call FarCall / db BANK / dw Func`) and the handlers it stores in RAM. A routine that reads the
  bytes after its call (it pops its return address, or reads it off the stack) returns past them:
  those bytes are its operands, shown as data. Tables, padding and what the code never reaches and
  reads as no code would are shown as **data**, so they are no longer functions; `read_data` reads
  them. Code nothing reached but that reads like code is still split as before, and followed when it
  calls two functions that were reached.

- The import reads which toolchain built the game from its start-up code (GBDK's, version by version). A game
  GBDK built is C, and its version says whether the pinned GBDK-2020 4.5.0 writes the same code; one no compiler
  built is assembly, for the assembly track.

## The matching lane

Matching is done with the pinned **GBDK-2020** toolchain: `sdcc -msm83` for C, `sdasgb` for assembly.
The app installs it when a game is imported (every download is pinned by sha256 per platform), and Setup lists it with
mgbdis and rgbds, which split the ROM.

Listings name the hardware the way [hardware.inc 6.0](https://github.com/gbdev/hardware.inc) does: `ldh [rLCDC], a`,
`ld [rROMB0], a` for the cartridge's bank register, `ld hl, $9800 ; TILEMAP0`. In C those are still addresses
(`*(volatile u8 *)0xff40`), the same bytes.

A Game Boy ROM's bytes contain **absolute** call and jump addresses, so the lane compiles in absolute
symbol mode: the build emits `.equ` lines that pin SDCC's references to the ROM's own addresses, and
scores the result with the same judge as every other platform.

An `rst` call with a byte after it is `void func_000_0028(void) __z88dk_shortcall(0x28, 0);` then `func_000_0028();`, which
builds to `rst $28` and the byte `0x00` (the second number is the byte that follows).

**Until the toolchain is installed**, a `.gb`/`.gbc` project is imported **read-only**: every function
is disassembled, searchable and briefable, but functions are not handed out for matching, because no
candidate could be judged yet. That is deliberate — it is the honest state rather than distributing
work nobody can score. Importing the game again (or installing GBDK-2020 from Setup) turns the lane on.

## What moves SDCC's output

The lane is new, so this list is short on purpose. Add what you measure with `add_note` (no function
name) so the next session starts ahead:

- Local declaration order and first use change register allocation — reorder before rewriting.
- Narrow types matter: SM83 arithmetic is 8-bit, and `u8`/`s8` locals versus `int` change the
  truncation SDCC emits.
- New projects build with `--fsigned-char` and no optimization flag, which is what GBDK's own `lcc` does. A game built
  another way may need `--opt-code-size` or `--opt-code-speed` (the project's compiler flags): if a function's loops or
  jumps come out in a different shape from the target, try those before rewriting, and say what worked in a note.

## Games written in assembly

Most Game Boy games are hand-written assembly, and SDCC matches only the parts of them it could have written. The app
sets the rest aside (status *assembly* or *not code*, with the reason): padding, graphics or tables the split took for
a function, routines that run on into the next one, code that reads a flag its caller left. A routine the split ran
on past its end is cut to it. If a function you're given looks like one of those, say so in a note and release it.

A routine set aside as hand-written is decompiled on the **assembly track** instead: `claim_function(track="asm")`
and document it in RGBDS assembly that rebuilds its bytes, with names instead of addresses (guide topic `asm`).

The split's boundaries can be wrong both ways. When a function you're given is two routines (one ends, the next
starts), `split_function(function, at=<CPU address where the second starts>)` cuts it. When it's half of one (it runs
on into the next function or jumps into it, or the one before runs on into it, and nothing else calls the second
half), `merge_function(function)` joins them. Each piece is checked again for what's set aside.

When the split took data for code, missed a function, or decoded the bytes after a call as instructions, say so with
`correct_split(kind, at, reason, count)`, with how you know (what reads it, what calls it, what it holds):

- `data`: `count` bytes at `at` aren't code (a table, graphics, text). `read_data` shows them first.
- `code`: a function starts at `at` (reached through a pointer the split can't follow).
- `table`: `count` handler addresses at `at` (a jump table nothing found).
- `operands`: the routine at `at` reads `count` bytes after each call to it and returns past them (`pop hl / ld
  a,(hl+) / ... / push hl / ret`, or a far call followed by its bank and address).
- `noreturn`: the routine at `at` never returns (a main loop, a reset).

`at` is a function's name, `NN:AAAA` (bank and address, hex) or an address in `function`'s bank. A correction doesn't
change anything at once: the next time a person splits the game again it does, and they can undo it first. Ten an
hour at most: they aren't a way round a hard function.

What SDCC does write in such a game, and how (each matched on a real cartridge):

- A bank switch around a call: `u8 b = (u8)(u16)&bank_0x11;` with `__at(0x0011) u8 bank_0x11;` gives `ld a, $11`,
  then `*(volatile u8 *)0xff9b = b; *(volatile u8 *)0x2100 = b;` and the call through a constant pointer:
  `((void (*)(void))0x4000)();`.
- A tail call to a fixed address: `((void (*)(void))0x2547)();` as a function's last statement gives `jp $2547`.
- An interrupt handler that only returns: `void handler(void) __critical __interrupt { }` gives `reti` (without
  `__critical`, `ei; ret`).

What it doesn't: a loop counted in `b` where SDCC picks `c`, `ld a, n` then `ld [nn], a` (SDCC writes
`ld hl, nn` then `ld [hl], n`), long runs of `ld [hl+], a` with constants. Don't spend many tries on those.

## Conventions

- `bankNNN_XXXX` style automatic names, `D_`/`B_` for data by address; keep the split's names unless a
  function's purpose is clear.
- Helper types and prototypes that several functions share go in the project's shared header
  (`get_context`), never edited per function to force a match.
