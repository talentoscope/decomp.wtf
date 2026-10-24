# C64 platform research (tap / t64)

Working notes for adding a Commodore 64 assembly track to decomp.wtf.
Test media: `MICROCOS.T64` (in `E:\DEV\retro\`). Research only — no game reversal done.

## 1. The C64 load model

- A C64 program "loads at address" X means its bytes are placed at memory X when run.
- Two tape flavours exist:
  - **TAP** — raw tape pulses; a single logical stream of blocks, one program per tape.
  - **T64** — a *container* with a directory of one or more PRG files, each carrying its own load address.
- On a real C64, pressing PLAY runs whatever BASIC/loader is resident and then the loaded block.
- A `.prg` file starts with a 2-byte little-endian load-address header; the program bytes follow. (MICROCOSM.prg is 19416 bytes = 2 + the 19414 data bytes in the T64.)

## 2. T64 container format (confirmed against MICROCOS.T64)

Header is **64 bytes**, then a directory of **32-byte entries**, then file data.

Header (64 bytes):
- `0x00`: signature string (e.g. `C64 tape image file`).
- `0x20`: version (LE, `$0100`).
- `0x22`: max directory entries (LE).
- `0x24`: **used entries** (LE) — how many real files follow.
- `0x28`: 24 bytes container/tape name.

Directory entry (32 bytes, at offset 64, repeated per used entry):
- `0x00`: entry type (1 = real file, 0 = unused).
- `0x01`: C64 file type (`$82` = PRG/machine code).
- `0x02`: **start / load address** (LE) — where the block goes in memory.
- `0x04`: end address (LE).
- `0x08`: **file offset** into the data section (32-bit LE).
- `0x10`: filename (16 bytes).
- Data length = end - start (or `data_size` field when present).

Verified on MICROCOS.T64:
```
[0] type=1 filetype=0x82 start=0x801 end=0x53d7 data_off=96 len=19414 name='MICROCOSM'
```
- 1 file, PRG, load address `$0801`, machine code, 19414 bytes, data at file offset 96.

## 3. Entry-point wrinkle (important for the plan)

The declared `$0801` start is **not** always clean code:
- MICROCOS.T64 bytes at `$0801` begin with an intro region (`"...2080 BROKEN BY TTF INC."`) followed by code.
- The BASIC stub is one line, `1992 SYS 2080 BROKEN BY TTF INC.`, so execution starts at `$0820` (2080).
- `$0820` copies 233 bytes (`$0833-$091B`) to `$0100-$01E8` (the stack page), does `SEI`, `STX $01` (=0, all RAM), resets SP and `JMP $0100`. `$0100` is the stack page, not BASIC.
- Interpretation: a cracked, **packed single-file** program. The code at `$0100` is a decruncher that reads packed data `$091C-$53D7` (markers `$CB`, `$87`) and rebuilds the real game in memory; the real program only exists after it has run.

Design takeaway: the load address from the T64 header is our starting hint, **not** a guarantee that code begins there. We must handle:
- an intro/BASIC-stub layer before the real code,
- the possibility that the substantive program is a *separate* block loaded at another address,
- resolving the true entry by following jumps (esp. the `$0801` autostart vector and any `$0100`/`$0801` JMP).

## 4. tapclean (block extraction tool)

- tapclean is a console utility (based on FinalTAP 2.76 Console).
- Usage: `tapclean <file.tap> -doprg` → creates a `prg/` folder with one extracted `.prg` per detected block.
- `-doprg` is the flag that **extracts each block as its own .prg file**.
- Without `-doprg` it only checks/reports.

Design takeaway: tapclean gives us, per block, the file bytes at their load address. We still need the **load address per block** to place code correctly — tapclean output ordering/filenames, or a parallel listing, must supply it.

## 5. What we need from a "parse + extract" step

For each block, produce:
- `load_address` (where it goes in memory),
- `bytes` (the raw program data),
- `is_machine_code` (filetype $82 vs BASIC text — matters for entry selection),
- the block's filename/label (for reference/debug).

Then feed each into the 6502 decode + function-sweep (mos6502) starting from the resolved entry address.

## 6. Open questions for the plan (for the user)

1. Should we target single-block PRGs (like a clean `$0801` game) first, or the loader/booter class shown here?
2. How do we robustly find the *real* entry when a `$0801` stub just `jmp`s to BASIC or loads a second block?
3. Do we trust T64 `$0801`/load addresses verbatim, or resolve via the autostart vector + call graph?
4. Should the byte-exact target be "the loaded program image in memory" (including the stub) or just the machine-code block?
5. Multiple blocks: do we decompile each address region independently, or treat the loader as separate?
