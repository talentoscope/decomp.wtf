# PS1 library identification

Project Settings → Compiler → **PSY-Q libraries** accepts user-provided signature JSON. Select an SDK version
label (for example `440`) and one or more per-library files. Uploaded files are validated and kept in the app's
local signature cache for future PS1 imports. No signature database is bundled or downloaded by this feature.

**Scan local signatures** previews labels and SDK-version evidence for the selected source unit, or all units
when Project default is selected. **Apply recognized libraries** names eligible
automatically named functions and sets them aside from agents. Custom names, existing matches, active claims and
functions a person explicitly returned to agents are preserved. Library metadata includes the original SDK symbol,
object names, candidate SDK versions, unit, address and file offset. It appears as the set-aside reason.

On subsequent PS1 imports, cached signatures are scanned before automatic compiler probes. Known library functions
are excluded from those probes, including libraries a person reopened for agent work. SDK-version evidence describes
library objects and can differ from the compiler used for game code. Tied SDK candidates remain tied; compiler and
assembler hints do not change project settings. Byte signatures do not establish C prototypes or parameter types.

The accepted per-library format is:

```json
[
  {
    "name": "synthetic.OBJ",
    "sig": "00 01 02 03 04 05 06 07 ?? ?? ?? ?? 0c 0d 0e 0f 10 11 12 13 14 15 16 17",
    "labels": [{"name": "example_library_function", "offset": 0}]
  }
]
```

Patterns need at least 24 bytes, 16 concrete bytes and a contiguous four-byte anchor. Wildcards are byte pairs
`??`; other tokens must be hexadecimal byte pairs. Labels must be C identifiers at aligned offsets inside the
pattern. Weak signatures are counted and skipped. There must be exactly one complete hit within a unit, and
conflicting names at the same location are reported rather than applied. A signature must cover a function's
whole recorded body before that function can be set aside.

Each overlay is scanned independently, including overlays that share a load address. Signature labels are bound
to both unit and file offset. Name collisions receive a unit prefix. Ambiguous or weak matches remain unresolved;
modified SDK objects may need signatures generated from the corresponding objects.

Scans have a time limit. Incomplete scans report provisional results and apply nothing. Uploads are limited to
64 files and 50 MiB per request; the local cache is limited to 256 MiB and 50,000 usable patterns.

The local HTTP endpoint `POST /api/libraries/psyq` accepts `slug`, optional `files` containing `{version, text}`
records, optional `unit`, and `apply`. Omitting files scans the local cache. The response is a job identifier; its result contains
labels, conflicts, version rankings, compiler hints and counts. Reports contain no signature bytes or generated C.
