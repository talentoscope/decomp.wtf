# Comparing GCC 2.x attempts

Use **Settings → Compiler → Compare compiler passes**, or the agent tool explain_difference(function, left, right). The authenticated operator endpoint is POST /api/compiler/diagnostics with slug, function, left and right. Both attempts must define the selected function. PS1 and PS2 ROM projects with a pinned GCC 2.x recipe are supported; custom commands and fork build systems are excluded.

The tool compiles both sources with the function's actual unit header, compiler, flags and assembler mode, adding only -da. The normal source restrictions, trusted GTE header, compiler sandbox and resource caps still apply. Source size is capped at 64 KiB per attempt, captured dumps at 4 MiB per compile, compile time at 20 seconds each within a 60-second request budget, and concurrent requests at two.

Each report lists captured pass hashes, equality or missing-output status, and a bounded diff at the first differing dump in GCC's pass order. Source-line notes are normalized; RTL IDs, registers and constants remain significant. Absolute quoted paths are represented by hashes so distinct path constants stay distinguishable. A dump absent on one side is unavailable, not equal.

The result identifies the first **observed** normalized difference. Compiler bookkeeping can still differ, and a difference at a pass boundary does not prove that pass caused the final assembly difference. The original binary has no original RTL to compare against. Continue judging candidates with the normal compile/link checks.

Local artifacts are saved under the project's diagnostics folder with git ignore rules. Settings, sources, matches, claims and targets are unchanged. WSL copies a fixed allowlist of dump filenames out before sandbox cleanup. The normal compile path captures nothing unless diagnostics is requested.

Dump names and meanings come from the [official GCC 2.95.3 manual](https://gcc.gnu.org/onlinedocs/gcc-2.95.3/gcc_2.html#SEC12); pass order was checked against [GCC's toplev.c](https://github.com/gcc-mirror/gcc/blob/releases/gcc-2.95.3/gcc/toplev.c). The supported PS1 compiler matrix is exercised with both native assembly and ASPSX emulation.
