"""Original synthetic CD sectors and fixed-extent disc export; no retail assets."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import disc_export as export
from decompnet.rom import sector_integrity as codec
from decompnet.rom.mapping import file_digest
from .test_cue import disc, sheet, transport
from .test_mapping import ADD, SUB, profile, region
from .test_overlays import EXE


@pytest.mark.parametrize('index,mode,expected', [
    (0, 1, '49f6e4ed1e0b892eb8789a9f5762feb1ee91ea3a14d1ff6debadc264d4a12d6e'),
    (1, 2, '0b8c5fde590d8a837eaaa02d7a0c91be809ad3d95cfc290dd8623b4f356ca6ec'),
    (2, 1, '5e47d118ea03573f3ea368ab75524fca9b7f046228008c2f8087dbbd2437268c'),
    (3, 2, '91bb89e907d13aedd55dc3c48ffd24260777af65c1d1b6e7746b1aef9d9af07f'),
])
def test_edc_ecc_independently_verified_sector_goldens(index, mode, expected):
    # Checked with upstream edccchk 2c59f198b4515cd87a8e7abe74c225af23058e98.
    raw = bytearray(2352)
    raw[:16] = b'\0' + b'\xff' * 10 + bytes([0, 0, 2, index, mode])
    if mode == 2:
        raw[16:24] = bytes([3, 1, 8, 0]) * 2
    skip = 16 if mode == 1 else 24
    raw[skip:skip + 2048] = bytes((i * 37 + 17) % 256 for i in range(2048)) if index < 2 else bytes(2048)
    result = codec.regenerate(raw)
    assert export.digest(result) == expected
    assert codec.regenerate(result) == result


@pytest.mark.parametrize('mode', ['MODE1/2048', 'MODE1/2352', 'MODE2/2336', 'MODE2/2352'])
def test_transport_updates_preserve_headers_and_unchanged_raw_bytes(mode):
    raw = transport(bytes(2048), mode)
    size, skip = __import__('decompnet.rom.cue', fromlist=['MODES']).MODES[mode]
    assert codec.update_sector(raw, bytes(2048), size, skip) == raw
    changed = codec.update_sector(raw, bytes([19]) * 2048, size, skip)
    assert changed[:skip] == raw[:skip] and changed[skip:skip + 2048] == bytes([19]) * 2048
    if size != 2048:
        assert changed[-276:] != raw[-276:]


def test_subchannels_and_invalid_modes():
    raw = transport(bytes(2048)) + bytes(range(96))
    out = codec.update_sector(raw, bytes([2]) * 2048, 2448, 24)
    assert out[2352:] == raw[2352:]
    for position in (0, 15, 20):
        bad = bytearray(raw[:2352])
        bad[position] ^= 1
        with pytest.raises(ValueError):
            codec.update_sector(bytes(bad), bytes([2]) * 2048, 2352, 24)
    bad = bytearray(raw[:2352]); bad[18] = bad[22] = 0x20
    with pytest.raises(ValueError, match='unsupported'):
        codec.update_sector(bytes(bad), bytes([2]) * 2048, 2352, 24)


def mapped_disc(tmp_path, mode=None, cue=False):
    contents = b'HEAD' + ADD + SUB
    iso = disc({'CODE.BIN': contents, 'OTHER.BIN': b'preserve me'})
    if cue:
        raw = transport(iso, mode)
        path = sheet(tmp_path, 'FILE "mixed.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
                     f'TRACK 02 {mode}\nINDEX 01 00:01:00\nFILE "audio.bin" BINARY\n'
                     'TRACK 03 AUDIO\nINDEX 01 00:00:00\n', {'mixed.bin': bytes(75 * 2352) + raw, 'audio.bin': bytes([37]) * 2352})
    else:
        path = tmp_path / 'disc.img'
        path.write_bytes(transport(iso, mode) if mode else iso)
    obj = profile(region('code', offset=4, file='CODE.BIN'))
    obj['input'] = {'format': 'psx-disc', 'sha256': file_digest(path)}
    obj['regions'][0]['source_sha256'] = export.digest(contents)
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'work')
    slug = net.mapping.create(path, json.dumps(obj), confirmed=True, log=lambda _: None)['slug']
    return net, slug, path


@pytest.mark.parametrize('mode,cue', [(None, False), ('MODE1/2352', False), ('MODE2/2336', False), ('MODE2/2352', True)])
def test_export_identical_then_edited_build_and_portable_rebuild(tmp_path, mode, cue):
    net, slug, path = mapped_disc(tmp_path, mode, cue)
    identity = file_digest(path)
    first = net.export_disc(slug, path, log=lambda _: None)
    root = Path(first['path'])
    assert first['identical'] and first['changed_sectors'] == 0
    assert file_digest(root / first['image']) == identity
    manifest = json.loads((root / 'disc-layout.json').read_text())
    assert all(not Path(v).is_absolute() for p in manifest['patches'] for k, v in p.items() if k == 'file')
    flat = root / 'project-image.bin'
    flat.write_bytes(SUB)
    second = net.export_disc(slug, path, flat, log=lambda _: None)
    assert not second['identical'] and second['changed_sectors'] == 1
    assert second['unchanged_bytes_verified'] and second['file_extents_preserved']
    assert file_digest(path) == identity
    portable = tmp_path / 'portable'
    run = subprocess.run([sys.executable, str(root / 'rebuild_disc.py'), '--base', str(path),
                          '--output', str(portable)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert file_digest(portable / first['image']) == second['output_sha256']
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    ignored = subprocess.run(['git', '-C', str(root), 'check-ignore', 'project-image.bin', first['image']],
                             capture_output=True, text=True, check=True)
    assert len(ignored.stdout.splitlines()) == 2
    if cue:
        assert (portable / 'image/audio.bin').read_bytes() == (tmp_path / 'audio.bin').read_bytes()
        assert (portable / first['image']).read_bytes() == path.read_bytes()
        assert (portable / 'image/mixed.bin').read_bytes()[:75 * 2352] == bytes(75 * 2352)


def test_layout_rejects_growth_bad_base_ranges_conflicts_and_existing_output(tmp_path):
    net, slug, path = mapped_disc(tmp_path)
    proj = net._project(slug)
    manifest = export.plan(net, proj, path)
    image, _ = net._rom(proj)
    out = tmp_path / 'out'
    with pytest.raises(ValueError, match='size'):
        export.rebuild(manifest, path, image + b'x', out)
    for patch in ({'size': 999999}, {'image_offset': -1}, {'file_offset': -1}):
        bad = copy.deepcopy(manifest); bad['patches'][0].update(patch)
        with pytest.raises(ValueError):
            export.rebuild(bad, path, image, out)
        assert not out.exists()
    bad = copy.deepcopy(manifest)
    bad['image_size'] = 16
    bad['patches'].append({**bad['patches'][0], 'image_offset': 8})
    with pytest.raises(ValueError, match='conflicting'):
        export.rebuild(bad, path, image + SUB, out)
    bad = copy.deepcopy(manifest)
    bad['gaps'] = [{'offset': 0, 'size': 8, 'sha256': export.digest(image)}]
    with pytest.raises(ValueError, match='unmapped'):
        export.rebuild(bad, path, SUB, out)
    path.write_bytes(path.read_bytes() + b'x')
    with pytest.raises(ValueError, match='identity'):
        export.rebuild(manifest, path, image, out)
    out.mkdir()
    with pytest.raises(ValueError, match='exists'):
        export.rebuild(manifest, path, image, out)


@pytest.mark.skipif(not EXE.exists(), reason='needs the original synthetic PS1 demo build')
def test_legacy_disc_import_identity_and_export_do_not_touch_user_sources(tmp_path):
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'work')
    first = tmp_path / 'one.iso'; first.write_bytes(disc({'BOOT.EXE': EXE.read_bytes(), 'OTHER.BIN': b'one'}))
    second = tmp_path / 'two.iso'; second.write_bytes(disc({'BOOT.EXE': EXE.read_bytes(), 'OTHER.BIN': b'two'}))
    slug = net.import_rom(first, confirmed=True, log=lambda _: None)
    assert net.import_rom(first, confirmed=True, log=lambda _: None) == slug
    assert net.import_rom(second, confirmed=True, log=lambda _: None) != slug
    root = Path(net._project(slug)['root'])
    src = root / 'source/user.c'; src.parent.mkdir(); src.write_text('/* keep my edits */')
    result = net.export_disc(slug, first, log=lambda _: None)
    assert result['identical'] and src.read_text() == '/* keep my edits */'
    with pytest.raises(ValueError, match='identity'):
        net.export_disc(slug, second, log=lambda _: None)


@pytest.mark.skipif(not EXE.exists(), reason='needs the original synthetic PS1 demo build')
def test_archive_overlay_patch_uses_its_original_file_offset(tmp_path):
    from .test_overlay_discovery import executable
    data = disc({'BOOT.EXE': EXE.read_bytes(), 'PACK.PAC': bytes(0x20000) + executable()})
    original = tmp_path / 'disc.iso'; original.write_bytes(data)
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'work')
    slug = net.import_rom(original, confirmed=True, log=lambda _: None)
    proj = net._project(slug)
    manifest = export.plan(net, proj, original)
    patch = next(p for p in manifest['patches'] if p['kind'] == 'overlay')
    assert patch['file'] == 'PACK.PAC' and patch['file_offset'] == 0x20800
    base, _ = net._rom(proj)
    edited = bytearray(base); edited[patch['image_offset'] + 4] ^= 1
    result = export.rebuild(manifest, original, bytes(edited), tmp_path / 'out', lambda _: None)
    assert result['changed_sectors'] == 1 and original.read_bytes() == data


def test_disc_export_operator_route_is_authenticated(tmp_path):
    import time
    from decompnet.app.server import build
    from starlette.testclient import TestClient
    net, slug, path = mapped_disc(tmp_path)
    mcp, _, security = build(net, tmp_path / 'home', 6499, 'test-secret')
    with TestClient(mcp.streamable_http_app(transport_security=security, host='localhost'), base_url='http://localhost:6499') as client:
        payload = {'slug': slug, 'original': str(path)}
        assert client.post('/api/disc/export', json=payload, headers={'X-Decomp': '1'}).status_code == 401
        client.cookies.set('decomp_live', 'test-secret')
        jid = client.post('/api/disc/export', json=payload, headers={'X-Decomp': '1'}).json()['job']
        for _ in range(100):
            result = client.get(f'/api/job?id={jid}').json()
            if result['state'] != 'running':
                break
            time.sleep(0.01)
        assert result['state'] == 'done', result
        assert result['result']['identical']
