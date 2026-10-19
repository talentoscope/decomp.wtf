# Manual mapping, milestone 1

A mapping profile tells decomp.wtf which bytes are executable and where they live
in memory. This helps games whose code is hidden in large resource containers or
loaded in banks that reuse an address. The profile is YAML or JSON data; it cannot
run commands. Version 1 supports PS1 binary files and ISO9660 BIN/ISO/CUE disc inputs. CUE sheets use their indexed data track; see [disc inputs](ps1-disc-inputs.md) for formats and bounds.

This milestone provides a CLI and an authenticated operator API. A mapping editor
in the frontend and profiles for other platforms can build on the same service.

## Profile format

```yaml
version: 1
platform: psx
title: My game
input:
  format: psx-disc       # or binary
  sha256: <64 lowercase hexadecimal characters for the entire input file>
gp: 0x80080000          # optional; omit/zero if unknown, which defaults to -G0
regions:
  - id: battle
    file: DATA/BATTLE.BIN  # omit for binary input
    source_sha256: <SHA-256 of the complete extracted disc file>
    offset: 0x800         # bytes within that file, NOT raw BIN offsets
    size: 0x1000
    vram: 0x80100000
    kind: code            # code or data
    visible: [resident]   # other regions this bank can see; explicit, not transitive
    confidence: verified  # verified or derived
    evidence: "Loader at ... reads ... sectors into ...; observed boundaries ..."
    functions:            # optional; offsets are relative to this region
      - offset: 0
        size: 0x40
        name: start_battle # optional C identifier; prefixed with the region id
        kind: code        # code or asm; asm marks a known handwritten function
  # ...declare resident here too...
```

Use independently inspected executable headers, loader instructions, relocation
records or a debugger's load trace as address evidence. A plausible disassembly
or a byte reconstruction alone does not establish the runtime address. `verified`
records the author's reviewed evidence; the app cannot prove its meaning. Hashes,
extents, alignment, function hints and instruction bytes are checked mechanically.
`derived` regions can be analysed but cannot be imported or applied.

The whole input hash pins a profile to a particular dump. For CUE, use the composite sheet-and-binaries identity from `mapping discover` or `mapping.file_digest`, rather than hashing the text alone. Each region also pins
its source container. A different input must have a separately reviewed profile;
do not change hashes to bypass a version mismatch. For binary input the two hashes
are identical. No game bytes need to be distributed with a profile.

RAM ranges may overlap in independent banks. They must not overlap inside one
region's visible set. Each region gets its own disassembly, memory reader, symbol
table, cross references and compilation context. Names have a `region__` prefix.
Where two functions occupy the same address, workers must use the complete name.
The `read_data` tool takes `unit` to select a region's view of memory.

Bounds in v1: at most 256 regions, 64 MiB total extracted region bytes, 64 MiB per
source disc file, 1 MiB profile text, cached PS1 RAM at `0x80000000..0x80200000`.
The configured GP applies to every region. Games that switch GP need a later
profile version or separate projects. CHD decompression and other container
transforms are outside this profile reader.

## Try an isolated project

These commands use an offline workspace. Its decomp.wtf server must be stopped;
the CLI refuses a workspace with a responding server. `validate` never opens its
database. Supply your own legally obtained input; the sample contains metadata only.

```bash
uv run decomp mapping validate profile.json --image '/path/to/game.bin'
uv run decomp mapping create profile.json --image '/path/to/game.bin' \
  --workspace /path/to/disposable-workspace --own-copy
```

The create result includes the project's slug. Start decomp.wtf with
`DECOMP_HOME=/path/to/disposable-workspace uv run decomp` to work on its functions
as usual. The default PS1 compiler is GCC 2.7.2 (`-O2`,
`-G8` with a supplied GP, otherwise `-G0`). This is a starting setting, not a claim
that every game or every region used that compiler.

Extend the profile with another verified region, retaining the old regions exactly:

```bash
uv run decomp mapping preview extended.json --image '/path/to/game.bin' \
  --workspace /path/to/disposable-workspace --project PROJECT_SLUG
uv run decomp mapping apply extended.json --image '/path/to/game.bin' \
  --workspace /path/to/disposable-workspace --project PROJECT_SLUG
```

A preview reports added regions/functions, preserved functions, derived regions
and changes that block application. Applying requires this project's workers,
autopilot, permuter and identical-function pass to have finished. Analysis writes
to scratch space. New targets are stored in an immutable `.mapping/<revision>`;
the database publishes the pointer and new rows in one transaction. A failed
transaction retains the old pointer and removes the unpublished revision.

Existing function IDs, targets, matches, attempts, notes and context versions stay
intact. V1 only adds regions to **profile-created projects**. Removing a region,
changing its address/bytes/visibility or changing existing function boundaries
requires a new project. Legacy imports remain unchanged; there is no migration of
their match history in this milestone. Profile symbol renames/splits, room sharing
and complete-disc export are disabled until they can preserve bank identity.

## Running-server API

`POST /api/mapping` uses the GUI's existing host check, `decomp_live` cookie and
`X-Decomp: 1` header. Worker bearer tokens do not authorize it. Request fields:

| action | additional fields | result |
| --- | --- | --- |
| `create` | `image`, `profile`, `confirmed: true` | background job ID |
| `preview` | `slug`, `image`, `profile` | background job ID |
| `apply` | `slug`, `image`, `profile` | background job ID |
| `profile` | `slug` | current normalized profile |
| `pause` | `slug` | stop requested for this project's managed workers/helpers |

`image` is a local path on the server; `profile` is the YAML/JSON text as a string.
Poll `/api/job?id=JOB_ID` for completion. `pause` is asynchronous: wait for sessions,
claims and helpers to finish before applying. Other projects can continue running.

## SLUS-00614 example and evidence

[The example](../examples/mapping/slus-00614.json) describes four complete
functions: two resident functions and two MAIN bank wrappers. It was written from
an owner's disc dump and static loader analysis. No third-party decompilation
repository code, names or mapping files were used. The generic implementation is
original work within decomp.wtf and uses its existing disassembly/compiler tools.

The expected raw disc has SHA-256
`2d6843c42ca595687b3780e20ab8c8873705ccead5c8bbc0b69d1fa70bc00a90`.
The included [evidence checker](../examples/mapping/verify_slus_00614.py) verifies the
input/container hashes, executable header, critical loader words, ISO extent and
the address arithmetic against a locally supplied image:

```bash
uv run python examples/mapping/verify_slus_00614.py --image '/path/to/SLUS-00614.bin' \
  --resident-only > /tmp/slus-00614-resident.json
uv run python examples/mapping/verify_slus_00614.py --image '/path/to/SLUS-00614.bin' \
  > /tmp/slus-00614-main.json
```

Create from the resident profile, then preview/apply the MAIN profile to exercise
the additive workflow. The evidence checker validates the static evidence used
here; it does not emulate the game or verify the completeness of the decompilation.

The executable header loads `SLUS_006.14` at `0x8002D000`, after its `0x800`-byte
header. Entry `0x80033930` establishes GP `0x80080994`, then calls `0x8003C9F8`.
The inspected function boundaries are `0x8003C9F8..0x8003CAC4` and
`0x8003CAC4..0x8003CB08`, corresponding to file bytes `0x101F8..0x10308`.

The MAIN mapping has a separate chain of evidence:

1. Resident routine `0x8004EB8C` queues type 6 with descriptor `0x80071644`.
2. The descriptor contains `0x0C020000`, `0x00000BD3`. Type 6 reaches the decoder
   at `0x8003E9A4`: low 23 bits select destination `0x20000`, shifted high bits
   select 24 sectors, and the second word selects starting LBA `0xBD3`.
3. The reader adds `0x80000000` to the destination. It converts LBA through
   `0x8006124C` (which adds the CD's 150-sector lead-in) and calls the read routine
   `0x80063224` with the count and destination.
4. ISO9660 places `MAIN/MAIN.BIN` at LBA `0x719`. Thus the loaded range is file
   `0x25D000..0x269000`, mapped to `0x80020000..0x8002C000`.
5. Two complete wrappers at file offsets `0x260004` and `0x26002C` therefore live
   at `0x80023004` and `0x8002302C`. Each occupies `0x28` bytes, including its return
   delay slot. Only these two functions are included in this first sample.

This is one statically verified bank, not a universal MAIN file base. Other MAIN,
TOWN and DUNGEON content needs its own evidence and region descriptions. The
sample does not replace an existing project, copy its history or
assert a complete gameplay mapping. Check compiler choices against exact matches
from each newly mapped area before expanding it.

## Discover archive candidates

```bash
uv run decomp mapping discover candidates.json --image '/path/to/SLUS-00614.bin'
```

Discovery writes a **new** candidate profile and prints scan counts. It never opens a workspace database and refuses to overwrite a profile. The operator API also accepts `action: discover` with an `image`; the job returns the proposed profile.

The scanner searches ISO files or a standalone archive/memory dump. Embedded PS-X EXE headers supply payload bounds and load addresses. For opaque raw members, 64 KiB windows locate JAL/prologue agreement; the proposed first/last instructions are estimated code extents, not proven archive directory entries. All generated regions are `derived`. Inspect the loader, archive index or a debugger load trace, correct extents/visibility and document the evidence before setting an individual region to `verified`. `create`/`apply` retain their existing verification requirements. No archive-specific index or decompressor is guessed.

Scanning is bounded to 64 MiB per source, 128 MiB total, 64 candidates and a 20-second analysis budget, checked between windows. Hashing the input precedes the analysis budget. Each source has at most 32 embedded executables. `truncated` reports exhausted source/byte/time/region limits; absence of candidates does not establish absence of code. Larger or compressed archives need a reviewed profile or extraction with user-supplied tooling. A memory dump can use a binary profile, with its observed RAM base and capture provenance recorded as evidence; the captured bytes remain the user's local input.

Ordinary disc import also recognizes embedded header-delimited payloads and scans complete loose overlay files up to 16 MiB, removing the former assumption that code begins in the first 64 KiB. Automatic JAL placement is bounded and rejects payloads outside RAM. Discovery provenance records ISO file, member offset, size, load address and method. Overlays sharing a load address remain independent units. Raw members with unknown boundaries are handled through the candidate workflow above.
