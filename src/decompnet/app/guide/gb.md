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

- Matching uses the pinned GBDK-2020 4.5.0 toolchain for every candidate; the importer does not identify the
  original compiler or prove that a routine was written in C. Hand-written code may simply fail to match.

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

The importer installs GBDK-2020 when possible. If it is unavailable, functions are still disassembled and
searchable but remain read-only because no candidate can be judged. Install GBDK-2020 and select it as the
project compiler to make those functions available for matching.

## What moves SDCC's output

The lane is new, so this list is short on purpose. Add what you measure with `add_note` (no function
name) so the next session starts ahead:

- Local declaration order and first use change register allocation — reorder before rewriting.
- Narrow types matter: SM83 arithmetic is 8-bit, and `u8`/`s8` locals versus `int` change the
  truncation SDCC emits.
- New projects build with `--fsigned-char` and no optimization flag, which is what GBDK's own `lcc` does. A game built
  another way may need `--opt-code-size` or `--opt-code-speed` (the project's compiler flags): if a function's loops or
  jumps come out in a different shape from the target, try those before rewriting, and say what worked in a note.

## Split limits and export

When available, `mgbdis` plus RGBDS produces a faithful bank-aware split and verifies the ROM rebuild. Otherwise
the built-in sweep is used; it is approximate and may miss code or include data as code. Function boundaries can
be corrected with `split_function` when a single candidate contains two functions. Imported data regions and
function starts are carried into the export as `.data` and `.code` blocks.

Most commercial Game Boy games contain assembly that GBDK-2020 cannot reproduce from C. A non-matching score is
not proof that the split is wrong. The current matching workflow judges C candidates; it does not provide an
in-app RGBDS assembly-editing track. Export creates an RGBDS project from the current split and names, keeps
matched C as reference, and checks that the unmodified project rebuilds byte for byte. It is a starting point for
external assembly work, translations and ports, not an in-app assembly matching workflow.

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

- `func_NNN_XXXX` automatic function names include the bank and CPU address; `D_`/`B_` name data by address.
  Keep the split's names unless a function's purpose is clear.
- Helper types and prototypes that several functions share go in the project's shared header
  (`get_context`), never edited per function to force a match.
