# The assembly track: documenting a hand-written routine

Most Game Boy, Master System, Game Gear and NES games were written in assembly, so for most of a game there is no C
to match. (The Game Boy's assembler is RGBDS; the Master System's and Game Gear's is WLA-DX, the NES's ca65: see the
end of this guide for what differs there.) A routine the app set
aside as hand-written (status *assembly*, a reason such as "reads hl before setting it") is decompiled by
**documenting** it: RGBDS assembly that rebuilds its bytes exactly, with names, comments and constants where the ROM
has addresses. It's kept as the project's source for that routine and goes into the exported disassembly.

`claim_function(track="asm")` hands you the next one (the smallest first); one asked for by name that's on the track
comes on it too. Send the whole routine with `compile_and_diff(function, source)`. The answer lists every check it
still fails; accepted, the function is **documented** under its label's name.

In a room, the host hands the routines out. A routine that passes every check on your copy goes to the host, which
judges it again on its own copy and keeps it for the room; members get the room's documented routines as they sync.
The host picks the ones a person looks over, and one sent back comes back on the track with what's wrong in a note.

## The six checks

1. **The same bytes.** It's assembled at the routine's address in its bank (rgbasm and rgblink, with hardware.inc
   6.0, its older names and the project's symbols already included). The answer shows the first instruction that
   differs, both ways.
2. **Every address is a name.** It's assembled again with the routine and every address the project knows moved:
   each call or jump target, memory operand, and 16-bit constant in ROM or RAM must move with it. Use the routine's
   own labels, the project's names (function names, names given with `rename_symbol`), or name an address yourself:
   `DEF wMenuOpen EQU $C0A2` (accepted, it becomes the project's). A 16-bit constant that isn't an address (a length,
   a count) is marked with a comment saying so: `ld bc, $0200 ; number: bytes to copy`.
3. **Banked code by the right name.** A call, jump or pointer into $4000-$7FFF names a function in the bank mapped
   there: the routine's own bank, or (from bank 0) the bank the split followed the call into (the brief lists them).
   Where banks share the address and no one knows which is mapped, it can't pass yet: say what you know with
   `correct_split`, or release it with a note.
4. **Instructions stay instructions.** `db` and `dw` only over the data the brief lists inside the routine (the bytes a
   routine reads after its call, a table) or an instruction a branch lands in the middle of.
5. **Hardware by name**, as hardware.inc 6.0 names it: `ldh [rLCDC], a`, `ld [rROMB0], a` (the bank register),
   `ld hl, TILEMAP0 + $21`, `ld de, _VRAM + $0800`. The older names (`rKEY1`, `rSVBK`, `LCDCF_ON`) work too.
6. **Real names, and a comment.** No automatic names (`Call_002_4a10`, `.l_4a20`, anything ending in an address):
   the routine's label says what it does, its local labels what they're for. Comment lines before the label say what
   it does, what it takes and what it returns, and which bank it maps if it maps one.

## Writing it

The brief has a **draft** that already rebuilds the bytes: the project's names filled in, hardware by name,
`db` over the data, a `.l_XXXX` label at each branch target. Start from it, or from the best attempt so far.

- RGBDS syntax: `ld a, [hl+]`, `ldh [c], a`, `ld hl, sp + 4`, `jr nz, .loop`, `rst $28`.
- One instruction a line; `db`/`dw` for data. Labels: `Name:` for the routine (and a second entry point), `.local:`
  inside it.
- What changes how the rest assembles, or reads files, is refused: INCLUDE, INCBIN, SECTION, MACRO, REPT, EQUS, OPT,
  IF and the like.
- Name what the routine touches from what it does with it: a byte it tests and branches on is a flag
  (`wMenuOpen`), a pointer it loads into HL a table (`MenuItemTable`). Read neighbours with `read_data` and the callers
  with the brief's notes; names the project already has carry over to every routine that uses them.
- A routine that turns out not to be one (data, two routines, half of one): `correct_split` or `flag_for_review`, as
  on the C track.

## Master System and Game Gear (WLA-DX)

The same six checks, in WLA-DX, the assembler these consoles' disassemblies are written for:

- Z80 syntax: `ld a, (wMenuOpen)`, `ld (MapperSlot2), a`, `jr nz, @loop`, `djnz @next`, `ex af, af'`. Data is
  `.db` and `.dw`. Labels: `Name:` for the routine, `@name:` for its own (a child of the routine's label); WLA-DX's
  anonymous `-:` and `+:` work too.
- Name an address with `.DEFINE wMenuOpen $C0A2` (a number). The project's functions are labels where they run, so
  `jr`, `djnz`, `jp` and `call` reach another routine by its name.
- Banks: slot 1 is at $4000, slot 2 at $8000. A call or pointer into the slot the routine runs from reaches its own
  bank; into the other slot, the bank that's mapped there. Where several banks have code there and nobody knows
  which, it can't pass yet: `correct_split` or a note.
- Hardware has no single standard set of names on these consoles; the judge's is:
  - ports: `PortVDPData` ($BE), `PortVDPControl` / `PortVDPStatus` ($BF), `PortVCounter` ($7E), `PortHCounter` /
    `PortPSG` ($7F), `PortIOA` ($DC), `PortIOB` ($DD), `PortMemoryControl` ($3E), `PortIOControl` ($3F),
    `PortFMAddress` / `PortFMData` / `PortFMControl` ($F0-$F2); on the Game Gear `PortGGStart` ($00),
    `PortGGLinkData`, `PortGGLinkDirection`, `PortGGSerialOut`, `PortGGSerialIn`, `PortGGSerialControl` ($01-$05),
    `PortGGStereo` ($06);
  - the mapper: `MapperControl` ($FFFC), `MapperSlot0`, `MapperSlot1`, `MapperSlot2` ($FFFD-$FFFF).
- A video memory address the code sends to the VDP (`ld hl, $3800`) is a number to the CPU: mark it
  `; number: the name table`, or `.DEFINE` a name for it.
- Refused: what reads files or changes how the rest assembles (`.INCLUDE`, `.INCBIN`, `.MACRO`, `.BANK`, `.ORG`,
  `.SECTION`...).

## NES (ca65)

The same six checks, in ca65, the assembler NES disassemblies are written for:

- 6502 syntax: `lda wTimer`, `sta PPUCTRL`, `lda (zPtr),y`, `jmp (wVector)`, `bne @loop` (`@name:` labels are local
  to the routine's label; ca65's unnamed `:` labels work too). Data is `.byte` and `.word`.
- ca65 writes an address below $100 in zero page (2 bytes). Where the ROM has the 3-byte form of a zero-page address,
  write `a:` before it: `lda a:zTimer`. The judge says so when that's what differs.
- Name an address with `wTimer = $0300` (a number). The project's names are defined for you; a call or jump to
  another routine is by its name.
- Banks: a call or jump into the window the routine runs from reaches its own bank; into a switched window, the bank
  mapped there. Where several banks have code there and nobody knows which, it can't pass yet.
- Hardware by the nesdev wiki's names: `PPUCTRL`, `PPUMASK`, `PPUSTATUS`, `OAMADDR`, `OAMDATA`, `PPUSCROLL`,
  `PPUADDR`, `PPUDATA`, `OAMDMA`, the APU's `SQ1_VOL` ... `SND_CHN`, `JOY1`, `JOY2` (`APU_FRAME` when written); MMC1's
  `MMC1_CONTROL`, `MMC1_CHR0`, `MMC1_CHR1`, `MMC1_PRG` and MMC3's `MMC3_BANK_SELECT` ... `MMC3_IRQ_ENABLE` (a register
  written anywhere in its range is its name plus an offset). A mapper written anywhere (UxROM, CNROM) has no register
  names: what the game writes to there is a table of its own, named as its data is.
- `brk` is written as the opcode alone, its byte after it with `.byte`.
- Refused: what reads files or changes how the rest assembles (`.include`, `.incbin`, `.macro`, `.segment`, `.org`...).

## Commodore 64 (py65)

The same six checks, assembled with py65 (it ships with the app; there is no ca65 step). Guide `c64` is the memory map and how a program was imported.

- 6502 syntax: `lda #$00`, `sta VIC_BORDERCOLOR`, `jsr CHROUT`, `lda CPU_PORT`, `bne loop`, `jmp (CINV)`. Data is `.byte` and `.word` (little-endian).
- A label on its own line belongs to the next instruction. Name an address with `wName = $033C`. The project's names are already defined.
- There are no cartridge banks. A `jsr` or `jmp` names the routine it reaches. A branch inside the routine is a label.
- Hardware, the processor port and KERNAL/BASIC calls use the map's names (`sta VIC_BORDERCOLOR`, `jsr CHROUT`). A store into `$A000–$BFFF` or `$E000–$FFFF` is RAM under the ROM when `$01 = $37` is not what the instruction wrote, so a store is not given a ROM routine's name. A `jsr`/`jmp` there is.
- `.org`, `.include` and macros are refused.
