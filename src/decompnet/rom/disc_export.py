"""Fixed-extent PS1 disc rebuilding from user-supplied assets; no filesystem repacking."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from . import cue, overlays, psx
from .mapping import file_digest
from .sector_integrity import update_sector

MAX_IMAGE = 256 << 20


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def source(path: Path, track: int | None = None):
    path = Path(path)
    if path.suffix.lower() == '.chd':
        raise ValueError('disc rebuilding needs an original BIN, IMG, ISO or CUE input')
    if path.suffix.lower() == '.cue':
        sheet = cue.read(path)
        selected = sheet.select(track)
        assets = [(path.name, path)]
        assets += list(dict.fromkeys((t.file.replace('\\', '/'), t.path) for t in sheet.tracks))
        with cue.TrackStream(selected) as stream:
            yield psx._Disc(stream), selected.file.replace('\\', '/'), selected.start, assets
    else:
        with path.open('rb') as stream:
            yield psx._Disc(stream), path.name, 0, [(path.name, path)]


def file_index(disc) -> dict:
    result = {}
    for name, lba, size in overlays.disc_files(disc):
        if name in result:
            raise ValueError('duplicate or multi-extent ISO filenames need an explicit supported layout')
        result[name] = (lba, size)
    return result


def read_slice(disc, lba: int, offset: int, size: int) -> bytes:
    within = offset % 2048
    return disc.read(lba + offset // 2048, within + size)[within:]


def gap_hashes(image: bytes, patches: list[dict]) -> list[dict]:
    result, cursor = [], 0
    for start, end in sorted((p['image_offset'], p['image_offset'] + p['size']) for p in patches):
        if start > cursor:
            result.append({'offset': cursor, 'size': start - cursor, 'sha256': digest(image[cursor:start])})
        cursor = max(cursor, end)
    if cursor < len(image):
        result.append({'offset': cursor, 'size': len(image) - cursor, 'sha256': digest(image[cursor:])})
    return result


def plan(net, proj, original: Path) -> dict:
    cfg = net.cfg(proj)
    if cfg.get('platform') != 'psx' or cfg.get('kind') == 'fork':
        raise ValueError('fixed-extent disc export requires a PS1 ROM project')
    image, segments = net._rom(proj)
    if len(image) > MAX_IMAGE:
        raise ValueError('project image exceeds the 256 MiB disc-patch limit')
    patches, headers = [], []
    profile = cfg.get('mapping', {}).get('profile')
    identity = file_digest(original)
    expected = profile['input']['sha256'] if profile else cfg.get('rom', {}).get('disc', {}).get('sha256')
    if expected and expected != identity:
        raise ValueError('original disc identity differs from the project input')
    track = cfg.get('rom', {}).get('disc', {}).get('track')
    with source(original, track) as (disc, _, _, _):
        if profile:
            if profile['input']['format'] != 'psx-disc':
                raise ValueError('a binary-only mapping has no disc-file placement evidence')
            for region in cfg['mapping']['regions'].values():
                patches.append({'file': region['file'], 'file_offset': region['offset'],
                                'image_offset': region['rom_start'], 'size': region['size'], 'kind': 'mapped region'})
        else:
            cnf = disc.find('SYSTEM.CNF', max_bytes=64 << 10)
            found = re.search(rb'BOOT\s*=\s*cdrom:?\\*([^\s;]+)', cnf or b'', re.I)
            boot = (found[1].decode('ascii', 'replace') if found else 'PSX.EXE').replace('\\', '/').lstrip('/').upper()
            program = (Path(proj['root']) / cfg['rom']['program']).read_bytes()
            original_program = disc.find(boot)
            if original_program is None or psx.read_exe(original_program)[0] != program:
                raise ValueError('the original disc program does not match this project')
            patches.append({'file': boot, 'file_offset': 0, 'image_offset': 0, 'size': len(program), 'kind': 'program'})
            headers.append({'offset': 0, 'size': 0x800, 'sha256': digest(program[:0x800])})
            origins = cfg.get('rom', {}).get('overlay_discovery', {}).get('sources', [])
            by_name = {s.name: s for s in segments}
            covered = set()
            for origin in origins:
                name = 'ovl_' + origin['name']
                if name not in by_name:
                    raise ValueError('overlay evidence no longer matches the project segments')
                patches.append({'file': origin['file'], 'file_offset': origin['offset'],
                                'image_offset': by_name[name].rom_start, 'size': origin['size'], 'kind': 'overlay'})
                covered.add(name)
            if any(s.name.startswith('ovl_') and s.name not in covered for s in segments):
                raise ValueError('overlay source extents are missing; reimport or use a verified disc mapping')
        index = file_index(disc)
        for patch in patches:
            key = patch['file'].replace('\\', '/').lstrip('/').upper()
            patch['file'] = key
            if key not in index or patch['file_offset'] < 0 or patch['size'] <= 0 or patch['file_offset'] + patch['size'] > index[key][1]:
                raise ValueError('a mapped patch exceeds its original ISO file extent')
            start, size = patch['image_offset'], patch['size']
            if start < 0 or start + size > len(image):
                raise ValueError('a patch exceeds the project image')
            patch['base_sha256'] = digest(image[start:start + size])
            if digest(read_slice(disc, index[key][0], patch['file_offset'], size)) != patch['base_sha256']:
                raise ValueError('disc member bytes differ from the project source evidence')
    return {'version': 1, 'base_sha256': identity, 'track': track, 'image_size': len(image),
            'patches': patches, 'gaps': gap_hashes(image, patches), 'headers': headers}


def outside_hash(path: Path, excluded: list[tuple[int, int]]) -> str:
    result, cursor = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for lo, hi in sorted(excluded) + [(path.stat().st_size, path.stat().st_size)]:
            if lo > cursor:
                stream.seek(cursor)
                left = lo - cursor
                while left:
                    block = stream.read(min(left, 1 << 20))
                    if not block:
                        raise ValueError('asset changed or became truncated during verification')
                    result.update(block)
                    left -= len(block)
            cursor = max(cursor, hi)
    return result.hexdigest()


def rebuild(manifest: dict, original: Path, image: bytes, destination: Path, log=print) -> dict:
    """Rebuild to a new directory from relative ISO placements and hashes, never commands."""
    original, destination = Path(original), Path(destination)
    if destination.exists():
        raise ValueError('disc output directory already exists')
    if manifest.get('version') != 1 or len(image) != manifest['image_size'] or len(image) > MAX_IMAGE:
        raise ValueError('rebuilt project image must retain its exact original size')
    if not isinstance(manifest.get('patches'), list) or not 0 < len(manifest['patches']) <= 4096:
        raise ValueError('layout needs between 1 and 4096 patches')
    for patch in manifest['patches']:
        if not all(type(patch.get(k)) is int for k in ('image_offset', 'file_offset', 'size')):
            raise ValueError('patch offsets and sizes must be integers')
        if patch['image_offset'] < 0 or patch['size'] <= 0 or patch['image_offset'] + patch['size'] > len(image):
            raise ValueError('patch exceeds the flat build')
    for region in [*manifest.get('gaps', []), *manifest.get('headers', [])]:
        start, size = region['offset'], region['size']
        if type(start) is not int or type(size) is not int or start < 0 or size < 0 or start + size > len(image):
            raise ValueError('invalid immutable region')
        if digest(image[start:start + size]) != region['sha256']:
            raise ValueError('an edited build changes unmapped padding or the executable header')
    before = file_digest(original)
    if before != manifest['base_sha256']:
        raise ValueError('original disc identity differs from the layout manifest')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.disc-', dir=destination.parent))
    try:
        with source(original, manifest.get('track')) as (disc, selected, track_start, assets):
            index = file_index(disc)
            resolved = []
            for patch in manifest['patches']:
                key, offset, size = patch['file'], patch['file_offset'], patch['size']
                if key not in index or offset < 0 or size <= 0 or offset + size > index[key][1]:
                    raise ValueError('a patch would grow or escape an original file')
                if digest(read_slice(disc, index[key][0], offset, size)) != patch['base_sha256']:
                    raise ValueError('original member bytes differ from the layout evidence')
                at = index[key][0] * 2048 + offset
                payload = image[patch['image_offset']:patch['image_offset'] + size]
                if len(payload) != size:
                    raise ValueError('patch image range is truncated')
                resolved.append((at, at + size, payload, key))
            resolved.sort(key=lambda row: row[0])
            for i, (lo, hi, data, _) in enumerate(resolved):
                for other_lo, other_hi, other, _ in resolved[i + 1:]:
                    if other_lo >= hi:
                        break
                    stop = min(hi, other_hi)
                    if data[other_lo - lo:stop - lo] != other[:stop - other_lo]:
                        raise ValueError('overlapping source regions request conflicting disc bytes')
            pending = {}
            for lo, hi, data, _ in resolved:
                for at in range(lo // 2048, (hi + 2047) // 2048):
                    old = disc.sector(at)
                    changed = bytearray(pending.get(at, old))
                    start, stop = max(lo, at * 2048), min(hi, (at + 1) * 2048)
                    changed[start - at * 2048:stop - at * 2048] = data[start - lo:stop - lo]
                    if changed != old:
                        pending[at] = bytes(changed)
                    else:
                        pending.pop(at, None)
            planned_files = {p['file'] for p in manifest['patches']}
            for name, (lba, size) in index.items():
                if name not in planned_files and any(lba * 2048 < (at + 1) * 2048 and at * 2048 < lba * 2048 + size for at in pending):
                    raise ValueError('a changed sector aliases an unplanned ISO file')
            log(f'copying {len(assets)} original disc asset(s)')
            for relative, path in assets:
                dest = staging / 'image' / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
            target = staging / 'image' / selected
            windows = []
            with target.open('r+b') as stream:
                for lba, data in sorted(pending.items()):
                    at = track_start + lba * disc.size
                    stream.seek(at)
                    raw = stream.read(disc.size)
                    changed = update_sector(raw, data, disc.size, disc.skip)
                    if changed[:disc.skip] != raw[:disc.skip] or (disc.size == 2448 and changed[2352:] != raw[2352:]):
                        raise ValueError('sector header or subchannel changed')
                    stream.seek(at)
                    stream.write(changed)
                    windows.append((at, at + disc.size))
            for relative, path in assets:
                target_asset = staging / 'image' / relative
                excluded = windows if relative == selected else []
                if path.stat().st_size != target_asset.stat().st_size or outside_hash(path, excluded) != outside_hash(target_asset, excluded):
                    raise ValueError('an asset changed outside the permitted data-sector windows')
            rebuilt_base = staging / 'image' / original.name
            with source(rebuilt_base, manifest.get('track')) as (rebuilt, _, _, _):
                if file_index(rebuilt) != index:
                    raise ValueError('ISO file extents or LBAs changed during rebuilding')
                for lo, hi, data, key in resolved:
                    offset = lo - index[key][0] * 2048
                    if read_slice(rebuilt, index[key][0], offset, hi - lo) != data:
                        raise ValueError('patched disc bytes do not reproduce the selected build')
        if file_digest(original) != before:
            raise ValueError('original assets changed during rebuilding')
        output_digest = file_digest(rebuilt_base)
        report = {'base_sha256': before, 'output_sha256': output_digest, 'identical': before == output_digest,
                  'changed_sectors': len(pending), 'integrity_regenerated_sectors': len(pending) if disc.size != 2048 else 0,
                  'file_extents_preserved': True, 'unchanged_bytes_verified': True, 'iso_files': len(index),
                  'image': 'image/' + original.name}
        (staging / 'project-image.bin').write_bytes(image)
        (staging / 'disc-layout.json').write_text(json.dumps(manifest | {'result': report}, indent=2) + '\n')
        (staging / 'rebuild_disc.py').write_text("from pathlib import Path\nfrom decompnet.rom.disc_export import main\nraise SystemExit(main(Path(__file__).parent))\n")
        (staging / '.gitignore').write_text('/*\n!/disc-layout.json\n!/rebuild_disc.py\n!/README.md\n!/.gitignore\n')
        (staging / 'README.md').write_text(
            '# Fixed-extent PS1 disc build\n\nImages remain local and are ignored by git. Keep your original assets.\n\n'
            'Run with decomp.wtf installed:\n\n    python rebuild_disc.py --base /path/to/original.cue --output /path/to/new-output\n\n'
            'Add --image /path/to/edited-project-image.bin for a new flat build. Image size, file extents and LBAs '
            'stay fixed. Growth, changed executable headers, conflicting regions and unmapped edits are rejected. '
            'The app verified matched C before its initial export; edited build behavior is not inferred. '
            'Changed raw Form 1 sectors get EDC/ECC; untouched audio, sectors and subchannels are copied exactly. '
            'This does not certify the original image or emulate the result.\n')
        staging.rename(destination)
        return {'path': str(destination), **report}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def export_disc(net, slug: str, original: Path, edited: Path | None = None, log=print) -> dict:
    proj = net._project(slug)
    if net.cfg(proj).get('platform') != 'psx' or net.cfg(proj).get('kind') == 'fork':
        raise ValueError('fixed-extent disc export requires a PS1 ROM project')
    if net._link(slug) is not None:
        raise ValueError('build disc images from the project host copy')
    rows = net.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj['id'])
    broken = net.regression_test(proj, None, only=rows,
                                 header_for=lambda fn: net._context(proj['id'], fn['unit'])['header'])
    if broken:
        raise ValueError(f'{len(broken)} matched functions no longer verify with current unit recipes')
    manifest = plan(net, proj, Path(original))
    base, _ = net._rom(proj)
    if edited is not None:
        if Path(edited).stat().st_size != len(base):
            raise ValueError('edited flat build must retain the project image size')
        image = Path(edited).read_bytes()
    else:
        image = base
    result = rebuild(manifest, Path(original), image, Path(proj['root']) / 'disc-exports' / uuid.uuid4().hex[:12], log)
    result['matched_functions_verified'] = len(rows)
    return result


def main(root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description='Rebuild a PS1 disc with fixed file extents and LBAs')
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = root or Path.cwd()
    try:
        manifest = json.loads((root / 'disc-layout.json').read_text())
        image_path = args.image or root / 'project-image.bin'
        if not 0 < manifest['image_size'] <= MAX_IMAGE or image_path.stat().st_size != manifest['image_size']:
            raise ValueError('flat build size differs from the layout manifest')
        result = rebuild(manifest, args.base, image_path.read_bytes(), args.output)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'disc build: {exc}')
        return 1
