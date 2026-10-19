"""Check the evidence for the small SLUS-00614 example against the owner's disc.

Original static analysis of a user-supplied dump. No game code or mapping was taken
from another decompilation repository. This script outputs metadata, never game bytes.
Run from a checkout with `uv run python examples/mapping/verify_slus_00614.py --image ...`.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from decompnet.rom.mapping import MappingError, Profile, digest, file_digest
from decompnet.rom.psx import _Disc


def verify(image: Path, resident_only=False):
    profile = Profile.parse(Path(__file__).with_name("slus-00614.json").read_text())
    if file_digest(image) != profile.sha256:
        raise MappingError("this example is for a different disc dump; do not transplant its addresses")
    with image.open("rb") as stream:
        disc = _Disc(stream)
        boot = disc.find("SLUS_006.14")
        main = disc.find("MAIN/MAIN.BIN")
        if boot is None or main is None:
            raise MappingError("the expected executable and MAIN container are missing")
        for region, blob in zip(profile.regions, (boot, main)):
            if digest(blob) != region.source_sha256:
                raise MappingError(f"{region.id}: unexpected container hash")
        pvd = disc.sector(16)
        root_lba, root_size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
        main_dir = next((e for e in disc.entries(root_lba, root_size) if e[0] == "MAIN" and e[3]), None)
        extent = next((e for e in disc.entries(main_dir[1], main_dir[2]) if e[0] == "MAIN.BIN"), None) if main_dir else None
        if extent is None or extent[1:3] != (0x719, len(main)):
            raise MappingError("MAIN's ISO9660 extent differs from the inspected dump")
        # Prove that file-relative sector arithmetic selects the same bytes on the disc.
        if disc.read(0xBD3, 0x18 * 2048) != main[0x25D000:0x269000]:
            raise MappingError("MAIN's bank extent disagrees with the disc")

    def require(condition, message):
        if not condition:
            raise MappingError(message)

    require(boot[:8] == b"PS-X EXE", "missing PS-X EXE header")
    entry, _, load, length = struct.unpack_from("<4I", boot, 0x10)
    require((entry, load, length) == (0x80033930, 0x8002D000, 0x54800), "unexpected executable header")

    def words(vram, expected):
        at = vram - load + 0x800
        actual = struct.unpack_from(f"<{len(expected)}I", boot, at)
        require(actual == expected, f"loader evidence differs at 0x{vram:08X}")

    words(0x800339AC, (0x3C1C8008, 0x279C0994))  # establish gp
    words(0x800339C4, (0x0C00F27E,))  # entry calls resident main
    # type 6; a1 = descriptor at 0x80071644; queue the read via 0x8003E4FC
    words(0x8004EB98, (0x24040006, 0x3C058007, 0x24A51644, 0x00003021, 0xAFBF0014, 0x0C00F93F))
    words(0x80071644, (0x0C020000, 0x00000BD3))
    # The queued type-6 branch reaches the descriptor decoder at 0x8003E9A4.
    words(0x8003E834, (0x24020006, 0x1062005A))
    words(0x8003E9F0, (0x3C03007F, 0x8E220000, 0x3463FFFF, 0x00438024,
                       0x16000005, 0x000295C2))  # mask destination; count = packed >> 23
    words(0x8003EA18, (0x3C028000, 0x02028025))  # cached RAM destination
    words(0x8003EAA4, (0x8E240004, 0x0C018493))  # descriptor LBA -> position converter
    words(0x80061254, (0x24840096,))  # converter adds CD's 150-sector lead-in
    words(0x8003EAE4, (0x02402021, 0x02002821, 0x0C018C89, 0x24060080))  # count, destination, read
    packed, lba = struct.unpack_from("<II", boot, 0x80071644 - load + 0x800)
    destination, size = 0x80000000 | (packed & 0x7FFFFF), (packed >> 23) * 2048
    start = (lba - extent[1]) * 2048
    mapped = profile.regions[1]
    require(start <= mapped.offset < mapped.offset + mapped.size <= start + size, "sample exceeds loaded bank")
    require(mapped.vram == destination + mapped.offset - start, "sample address disagrees with the loader")
    for region in profile.regions:
        if region.file == "SLUS_006.14":
            require(region.vram == load + region.offset - 0x800, "resident address disagrees with the header")
    result = profile.to_dict()
    if resident_only:
        result["regions"] = result["regions"][:1]
    print("Executable header, GP, loader descriptor, ISO extent and sample addresses verified.", file=sys.stderr)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--resident-only", action="store_true", help="output the initial profile for an additive rescan demo")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.image, args.resident_only), indent=2))
    except (MappingError, OSError, StopIteration, struct.error) as error:
        parser.exit(1, f"verification failed: {error}\n")
