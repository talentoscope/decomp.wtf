# PS1 CUE and sector input

Use **Use a local disc or CUE path** on the new-project page to select a CUE sheet. Keep its binary tracks under the sheet's folder. The reader follows FILE/TRACK declarations and INDEX 01 offsets, skips audio tracks, and looks for a readable PlayStation program. It does not assume Track 1 contains data. If several data tracks are bootable, enter a track number in the dialog's CUE options. The import API accepts the same optional `cue_track` query parameter.

A browser upload of the sheet alone cannot supply its referenced tracks; the page directs it to the local-path workflow. Binary filenames may be quoted, contain spaces/backslashes, and use a unique case-insensitive filesystem match. Missing files, path escapes, unsupported commands and ambiguous track selection produce errors.

Supported binary modes are AUDIO for track layout, MODE1/2048, MODE1/2352, MODE2/2352, MODE2/2336 and cooked MODE2/2048. Raw 2336-byte input is also detected directly, including `.img` files, by its ISO9660 content. Filename extension alone does not determine a binary image's format. Sector decoding remains a data reader; it does not certify EDC/ECC or copy protection.

INDEX times are offsets within their FILE, with 75 frames per second. INDEX 00 describes stored pregap data; PREGAP/POSTGAP describe missing generated sectors and do not add bytes to a file offset. Track views end before the next track's stored pregap/data, preventing file extents from reading into audio. Declared modes are checked rather than replaced by a different geometry guess. Truncated sectors, invalid extents, malformed ISO records and missing data fail explicitly.

Bounds: 1 MiB sheets, at most 99 tracks and unique binary FILE entries, ordered indices, and whole stored sectors. Different sector widths may use separate files; mixed widths within one file are rejected. Nonbinary audio encodings, CDTEXTFILE assets, compressed tracks and unusual multisession layouts whose filesystem addresses are not relative to the selected data track need conversion or another supported input. These limits do not imply that unrecognized files lack code.

Manual mapping and archive discovery also accept CUE sheets with one bootable data track. For CUE, `input.sha256` is a composite identity: SHA-256 over `decomp-cue-v1` plus a zero byte, the exact sheet bytes, and the 32-byte SHA-256 digest of each referenced binary in first-track order. Use `decompnet.rom.mapping.file_digest(Path(...))` or `mapping discover` to compute it. Source-container hashes still validate extracted ISO files. Changing an audio binary invalidates the input identity too.

Format facts: [GNU CUE INDEX reference](https://www.gnu.org/software/ccd2cue/manual/html_node/INDEX-_0028CUE-Command_0029.html), [GNU track modes](https://www.gnu.org/software/ccd2cue/manual/html_node/MODE-_0028Compact-Disc-fields_0029.html), and [PSX-SPX disc images](https://psx-spx.consoledev.net/dev/ps1/cdr/cdromfileformats/diskimages/). Readers are original code; no third-party parser or retail image is bundled.

Fixed-extent output is available through **Build disc image**; see [disc export](ps1-disc-export.md) for preserved extents, supported sector formats and verification.
