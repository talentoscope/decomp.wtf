# PS1 function evidence in briefs

`get_function` and claim briefs include an instruction-evidence summary alongside the effective unit compiler/assembler settings and existing caller/data references. It reports direct and indirect link calls, non-return indirect jumps, GTE instructions, observed entry-frame allocation and saved registers. A leaf indication means no link-call instruction was observed in the bounded code, not that the routine has no tail transfer.

The entry block tracks constant and stack-relative values through supported instructions, up to its first transfer including the delay slot, or 256 instructions. Saved-register offsets are relative to the incoming stack pointer. Dynamic stack writes or unknown instructions leave the frame unresolved. Argument observations are registers read before being written and potential incoming stack loads at entry-SP offsets of 16 or more. They are lower bounds: forwarded, unused and later-block parameters can be missing. No parameter count or C prototype is asserted.

Resolved accesses to scratchpad and I/O address ranges include the instruction offset, address and access width. Unknown bases and clobbered constants produce no resolved-access claim. Range membership does not identify a specific register or prove that an access is valid. Address-range reference: [PSX-SPX memory map](https://psx-spx.consoledev.net/memorymap/).

Jump-table evidence identifies references to named table symbols, with the method and uncertainty stated. Neither table bounds nor its relationship to an indirect dispatch is proven by the name. Indirect jumps alone do not become jump tables.

Whole-code counts are bounded to 4,096 complete instructions; longer/truncated inputs carry a partial-evidence indication and no leaf verdict. Summaries do not change targets, context, names, eligibility or matching decisions. Synthetic tests cover frames, delay slots, saved registers, partial argument evidence, calls, memory resolution, clobbers, unknown instructions and bounded analysis.
