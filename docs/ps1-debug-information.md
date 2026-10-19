# PS1 debug information

Settings → Compiler → Debug information accepts user-supplied PsyQ MND version 1 `.SYM`, text `.MAP`, and `.CPE` files. Select a memory unit when overlays share addresses. Preview first to review the names, rejected addresses and build confidence, then apply the evidence. Debug files found on an imported disc are inspected automatically; an unsupported or malformed file leaves the program import usable.

A name must map to exactly one selected unit. Function records must start at an existing function boundary. Conflicting names and unmapped or ambiguous addresses are skipped. CPE load records must fit a unique region and match its bytes exactly; mismatches reject the entire operation. CPE alone supplies load/entry evidence, not symbols. Address agreement without CPE does **not** prove that a symbol file belongs to this build. A partial CPE validates only the bytes it contains.

Names are added as aliases at their original addresses, preserving canonical function names, matched C, contexts and compiler settings. Claimed functions are skipped. Alias names cannot replace a name bound to another address. Aliases resolve during matching and become absolute linker symbols in exports; splat continues to use canonical names.

Function briefs retain sanitized source basenames, line/frame information, stack/register homes, and recognized declaration proposals. Original directory paths are discarded. Common scalar, pointer, array and function types are decoded; unknown codes stay evidence. Type records are bounded and never silently inserted into shared C context. Review proposals through the usual context-edit/regression workflow. Function boundaries and translation-unit identity remain evidence rather than automatic changes to memory mapping.

Readers reject truncated records, unknown binary chunks, excessive dimensions, files above 32 MiB, more than 200,000 records, and uploads above 64 files/64 MiB. Text maps accept address/name rows, name/address assignments and PsyQ section rows. Unsupported map dialects need conversion to address/name rows. Names in unmapped BSS need an established mapping before import. No debug database or retail asset is distributed.

The local API is `POST /api/debug/psx`, with `slug`, optional `unit`, `apply`, and `files: [{name, data}]`; `data` is base64. Results are bounded summaries. The engine also accepts `(filename, bytes)` pairs through `import_psx_debug`. Format reference: [PSX-SPX file formats](https://psx-spx.consoledev.net/cdromfileformats/).
