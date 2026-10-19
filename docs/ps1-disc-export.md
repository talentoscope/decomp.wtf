# Fixed-extent PS1 disc export

Choose **Build disc image** in the source panel and provide the original local BIN, IMG, ISO or CUE path. Keep every binary track referenced by a CUE sheet beside it. The original assets are read only; a new copy is saved in the project's disc-exports folder. No large disc assets are retained at import.

Leaving the optional edited flat build blank verifies current matched C against the project targets with each unit's effective recipe and reproduces the imported bytes. For modifications, first build an edited source export, then select its flat output. Disc export does not regenerate or overwrite the source tree.

The exporter patches the boot executable and discovered overlay payloads at their recorded file offsets, or the verified regions of a psx-disc mapping. File names, file sizes and LBAs remain fixed. Archive bytes outside the recorded payloads, trailing executable bytes, audio, pregaps and other tracks are copied. File growth, missing overlay placement evidence, conflicting regions, modified executable headers and changes to unmapped flat padding are rejected. A binary-only mapping needs disc placement evidence before it can be exported.

Changed Mode 1 or Mode 2 Form 1 raw sectors receive EDC/ECC. The original implementation solves the two parity equations over GF(256); physical geometry follows [PSX-SPX's format reference](https://psx-spx.consoledev.net/cdromformat/). Synthetic sector goldens were checked independently with [edccchk](https://github.com/claunia/edccchk) at revision 2c59f198b4515cd87a8e7abe74c225af23058e98. That external checker is not bundled. Cooked 2048, Mode 2/2336, raw 2352 and 2448 with preserved subchannels are supported. Form 2 writes and CHD output are unsupported.

Verification compares the ISO file extent index, patched payloads, every asset's size, all bytes outside changed sectors, and the original input's fingerprint before and after copying. Untouched sectors retain their original integrity bytes, including unusual or invalid ones. This proves the stated copy/patch properties; it does not certify the original image or emulate modified behavior.

New imports bind the full disc input hash, including all CUE assets, so identical boot executables on different discs remain separate projects. Older imports bind a supplied original through their executable and every recorded overlay payload before pinning its hash.

Each output includes a portable disc-layout.json, rebuild_disc.py and ignored project-image.bin. With decomp.wtf installed, run:

    python rebuild_disc.py --base /path/to/original.cue --output /path/to/new-output

Add --image /path/to/edited-build.bin to select another same-size build. Existing output directories are rejected. Images and flat builds are ignored by the generated git rules; the layout manifest contains relative member names and hashes, without original asset paths.
