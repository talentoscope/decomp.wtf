# objdiff symbol and jump-table pairing

Fork diffs use objdiff's reported target-to-base symbol indices rather than assuming both objects use the same names. The index zero is valid. Invalid explicit pairings remain unpaired. This applies to per-function scores, unit function scores and the extra loaded-constant check.

Jump-table labels in the unit are displayed with their actual paired symbol and data score. A missing pairing or missing data rows mean unmeasured data, never an all-zero section. These unit data results are separate from an individual function's instruction score. A real constant mismatch still prevents a function match, including when its function and data symbols are mapped to other names.

Keep generated-label handling in the upstream project's build configuration. The app does not rename or hide labels, zero sections, or rewrite target objects. For projects with differently named table symbols, objdiff supports per-unit symbol_mappings from target to base, as documented by its [pinned schema](https://github.com/encounter/objdiff/blob/v3.8.2/config.schema.json). Correct table placement and whole-object linking still require the project's normal build checks.

A paired jump-table data mismatch also blocks a function that directly references that table, including changes after its first entry. Unrelated tables in the same unit do not block the function, and unpaired/unmeasured data is not treated as a known mismatch.
