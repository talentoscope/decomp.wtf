"""Discover reviewable PS1 archive/memory-dump mappings without changing matching targets."""
from __future__ import annotations

import hashlib
import struct
import time
from pathlib import Path

from . import overlays, psx
from .mapping import file_digest
from .n64code import _frame, _place

WINDOW = 64 << 10
MAX_REGIONS = 64


def candidates(data: bytes, deadline: float) -> list[dict]:
    if time.monotonic() >= deadline:
        return []
    found = [{"offset": offset, "size": len(code), "vram": vram,
              "evidence": "Embedded PS-X EXE header establishes this payload extent and load address; review loader use."}
             for offset, code, vram in overlays.embedded_exes(data)]
    for at in range(0, len(data), WINDOW - 0x1000):
        if time.monotonic() >= deadline or len(found) >= MAX_REGIONS:
            break
        end = min(at + WINDOW, len(data)) & ~3
        if end - at < 0x100 or any(r['offset'] < end and at < r['offset'] + r['size'] for r in found):
            continue
        block = data[at:end]
        if not overlays._code_like(block, 8):
            continue
        words = struct.unpack(f'<{len(block) // 4}I', block)
        if sum(_frame(w) for w in words) * len({w for w in words if w >> 26 == 3}) > 2_000_000:
            continue
        placed = _place(overlays._swap(block), 0, len(block), 0, [], [], regions=(overlays.RAM['psx'],))
        if placed is None:
            continue
        start, stop, vram, votes = placed
        if not 0x80000000 <= vram < vram + stop - start <= 0x80200000:
            continue
        found.append({'offset': at + start, 'size': stop - start, 'vram': vram,
                      'evidence': f'{votes} distinct JAL targets agree with prologues in this window. '
                                  'Code boundaries and load base are inferred; verify loader or trace evidence before import.'})
    return sorted(found, key=lambda row: row['offset'])


def discover(image: Path, seconds: float = 20) -> dict:
    """Hash the input, then scan bounded sources. Every emitted region is derived and needs operator review."""
    image = Path(image)
    digest = file_digest(image)
    deadline = time.monotonic() + max(0, min(seconds, 120))
    scanned, truncated, regions, total_regions = 0, False, [], 0
    from .cue import open_image

    with open_image(image) as stream:
        is_disc = psx.is_disc(stream)
        disc = psx._Disc(stream) if is_disc else None
        if disc is None:
            stream.seek(0)
        sources = overlays.disc_files(disc) if disc else [('', 0, image.stat().st_size)]
        for filename, lba, size in sources:
            if time.monotonic() >= deadline or len(regions) >= MAX_REGIONS:
                truncated = True
                break
            if size > overlays.MAX_ARCHIVE or scanned + size > overlays.MAX_SCAN:
                truncated = True
                continue
            data = disc.read(lba, size) if disc else stream.read(size)
            if len(data) != size:
                raise ValueError('source extent is truncated')
            scanned += len(data)
            source_hash = hashlib.sha256(data).hexdigest()
            for candidate in candidates(data, deadline):
                if len(regions) >= MAX_REGIONS or total_regions + candidate['size'] > 64 << 20:
                    truncated = True
                    break
                total_regions += candidate['size']
                regions.append({**candidate, 'id': f'region_{len(regions):03d}', 'source_sha256': source_hash,
                                'kind': 'code', 'confidence': 'derived', 'visible': [],
                                **({'file': filename} if disc else {})})
        truncated |= time.monotonic() >= deadline
    profile = {'version': 1, 'platform': 'psx', 'title': 'PS1 discovery candidates',
               'input': {'format': 'psx-disc' if is_disc else 'binary', 'sha256': digest}, 'regions': regions}
    return {'profile': profile if regions else None, 'candidates': len(regions), 'scanned_bytes': scanned,
            'truncated': truncated, 'note': 'Derived candidates cannot be imported until their loader evidence and extents are reviewed.'}
