# PS1 BIOS dispatch wrappers

PS1 import and the existing assembly-classification pass recognize exact small wrappers that load `$t2` with `0xA0`, `0xB0` or `0xC0`, jump through `$t2`, and supply the BIOS slot in `$t1`. The recognizer supports a slot in the jump delay slot and either literal-load order followed by an explicit nop delay slot. Only `addiu`/`ori` from `$zero` are accepted. Additional instructions, missing delay slots, register-sourced values and destinations outside these gates are rejected. Trailing nop alignment is bounded to a 32-byte routine.

Names include the gate and slot, such as `bios_A0_2A_memcpy`, so BIOS calls do not collide with ordinary C library names. A curated set of common stock BIOS API labels is included; other slots retain a numeric name. The implementation stores the exact dispatch evidence and a caveat: runtime BIOS table patches can change the destination. No C prototype is inferred.

Unworked automatic names can be replaced in bulk, retaining the old name as an address alias. Existing C, context declarations and call addresses remain valid. Matched functions, drafts and custom names retain their names; active claims, human review and explicit agent overrides are skipped. Recognized wrappers are classified as library/system assembly and excluded from automatic compiler probes. Mapping profile names and linked room copies remain stable. Independent overlapping units get distinct names, with ownership retained for alias lookup.

Labels and dispatch conventions follow the factual API table in [PSX-SPX Kernel BIOS](https://psx-spx.consoledev.net/kernelbios/). No BIOS bytes, Sony library objects or third-party signature database are bundled. Tests construct original synthetic wrappers and near misses.
