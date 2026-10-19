# Contribution policy evidence

The existing pre-fork policy reader now recognizes LLM_POLICY files and singular doc/ directories, alongside README, CONTRIBUTING, AGENTS, AI_POLICY and the other supported root, .github/ and docs/ files. Explicit policy files are read before READMEs. AI statements retain their source filenames, revision and content fingerprint; the yes/no/rules labels remain reading aids, not authoritative permission.

Reads are limited to 32 files of at most 400000 bytes each. Oversized, unreadable and external linked files appear as skipped, with an incomplete-review message. Partial evidence remains visible. Scanning does not execute repository content.

Forks retain a review in their project configuration and show it in the source panel. **Review current local rules** refreshes it from actual working files, including uncommitted policy edits. Room members independently read their own checked-out project rules.

Before the upstream command writes or commits into an existing destination containing AI statements, it displays those statements and requires --acknowledge-policy. That acknowledges a concrete review and does not override prohibitions. Destinations with no detected statements need no extra acknowledgement. This export remains local; pushing and sending contributions are separate actions.

Existing README and license files are preserved. Reconstruction notices use DECOMPWTF.md when the destination already has its own README or has added contribution rules to a previously generated README. The generated provenance sentence alone does not trigger its own policy review on every repeated export.
