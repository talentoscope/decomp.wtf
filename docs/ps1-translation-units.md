# PS1 translation-unit exports

PS1 export can compile several matched functions in one C translation unit. Debug source basenames propose groups automatically; the basename is provisional provenance, not proof of an original compilation boundary. An operator can propose groups for one export with `POST /api/export`:

```json
{"slug":"PROJECT","translation_units":[{"name":"unit.c","functions":["FirstRoutine","SecondRoutine"]}]}
```

Omit `translation_units` to use debug evidence, or pass `[]` to retain individual objects. A proposed group needs 2–128 known functions within one memory unit, the same effective compiler profile/context, and verified C for every member. At most 64 groups and 64 KiB of combined source are considered. A basename is used for source provenance; generated paths are sanitized and include a stable digest.

The exporter compiles functions together in image order and requires:

- The native object contains exactly the proposed functions in one `.text` section.
- Every function's native offset reproduces both its original RAM and image offset.
- Each function still matches through the ROM linker, including referenced data.
- Shared constant/data references yield one consistent placement per section.
- Inter-function padding agrees with the image; unmapped trailing code/data is rejected.
- The complete exported project rebuilds byte for byte.

A successful group becomes a real source file under `src/units/`, compiled once into a shared object. `build.json` records member order/offsets, section alignment, padding, data placements and provenance. The linker places shared text once and asserts function addresses. Verified private sections use their exact mapped offsets rather than imposing a compiler section alignment on the original layout; original labeled assembly supplies their data bytes.

Individual C files remain as fallbacks. Compile/layout failures retain individual objects and record a reason. If the combined complete-image rebuild fails, the exporter restores the verified individual build and checks it again; objdiff paths follow the active objects. This supports useful grouping without claiming historical TU identity or weakening byte verification. Unmatched members, incompatible padding, newly inlined code and conflicting pooled constants can prevent grouping.

`python build.py check NAME` checks every member of an active shared source and publishes new layout/data metadata only if all agree. Changing an unselected member can therefore fail the check. Removing a group source uses individual files; a successful individual check then records that choice for the whole former group. `python build.py` remains the final complete-image check. Existing mapping-profile exports retain their current restrictions; these groups organize ordinary PS1 exports.

Validation uses original synthetic compiler/object fixtures and the repository's owned PS1 demo. It covers native grouping, a clean rebuild, automatic source evidence, member edits, absent group files, conflicting constants, incompatible offsets/padding and failed-combined-build fallback. No retail source, image or debug data is included.

Source export keeps overlapping overlays in separate disassembler symbol contexts. Function hints carry their flat-image offsets, and generated overlay data labels include the segment name. Older automatic overlay layouts receive these isolation settings in the exported copy.
