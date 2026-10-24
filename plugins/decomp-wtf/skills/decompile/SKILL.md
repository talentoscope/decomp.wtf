---
name: decompile
description: Decompile a game the user owns (ROM, disc image, ISO, executable) into matching C with decomp.wtf. Use when the user asks to decompile, decomp, reverse engineer, port or mod a game, or to match a game's functions byte for byte (N64, PlayStation 1 and 2, GameCube, Wii, Game Boy Advance, DS, Commodore 64, or a public decomp of any platform). A Commodore 64 program is documented as 6510 assembly (claim_function with track asm), not matched from C.
---

# Decompiling a game with decomp.wtf

decomp.wtf runs on the user's computer and gives you everything matching decompilation needs: the game's original
compiler, a judge that compiles your C and compares it with the game byte for byte, free matches from machine
drafts, helper agents working in parallel, and a live view for the user. Its tools come from the `decomp` MCP server.

1. **decomp.wtf installed?** If the `decomp` tools are unavailable, ask the user whether to install decomp.wtf
   (free, open source, local): `curl -fsSL https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.sh | bash` on Linux, or
   `irm https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.ps1 | iex` in PowerShell. Then restart this session so the tools load.
2. **The game.** Work only with the user's own copy. Never download games or point to download sites. Ask the user
   to confirm the file is their own legally obtained copy, then `import_game(path, own_copy=true)`. If the game is
   already there, `list_projects()` and `open_project()`.
3. **Show it.** `open_gui()` opens the live view: the function map, every agent's work, diffs, progress.
4. **Work.** `claim_function()` gives you a function with its assembly, notes and a machine draft;
   `compile_and_diff(function, source)` until the score is 0 (byte-identical). Read `guide("workflow")` first and
   the compiler guide it points to. Share what you learn: `edit_context` for types and prototypes, `rename_symbol`
   for names, `add_note` for hints. Stuck after ~20 compiles: `release_function` with notes.
5. **Scale up.** For a big game, `start_agents(count)` adds helper agents; `progress()` shows everyone's work.
6. **Deliver.** `export_project()` writes a normal decomp project that rebuilds the game byte for byte: the base for
   ports and mods.

Report progress as functions and % of code matched. The game's code belongs to its rights holder: never publish it.
