"""Archive fixtures are original synthetic code; proposed raw extents remain derived."""
import json
import struct
from types import SimpleNamespace


from decompnet.rom import overlay_discovery as discovery, overlays, psx
from decompnet.rom.mapping import Profile
from .test_overlays import _iso, _overlay

BASE = 0x80180000


def executable(base=BASE):
    code = _overlay(base, 40)
    header = bytearray(0x800)
    header[:8] = b'PS-X EXE'
    struct.pack_into('<IIII', header, 0x10, base, 0, base, len(code))
    return bytes(header) + code


def test_embedded_executables_past_first_64k_have_exact_bounds_and_shared_bases():
    prefix = bytes(0x20000)
    exe = executable()
    archive = prefix + exe + bytes(0x1000) + exe
    found = overlays.embedded_exes(archive)
    assert len(found) == 2
    assert found[0] == (len(prefix) + 0x800, exe[0x800:], BASE)
    disc = psx._Disc(_iso({'PACK.PAC': archive}))
    report = {}
    found = overlays.find(disc, 'BOOT.EXE', 'psx', (0x80010000, 0x80020000), report)
    assert len(found) == 2 and found[0][1] == exe[0x800:] and found[0][2] == BASE
    assert report['sources'][0]['offset'] == len(prefix) + 0x800
    assert report['sources'][1]['method'] == 'embedded PS-X EXE header'


def test_loose_code_after_large_header_is_no_longer_discarded():
    data = bytes(0x20000) + _overlay(BASE + 0x20000, 40)
    found = overlays.find(psx._Disc(_iso({'LATE.BIN': data})), 'BOOT.EXE', 'psx', (0x80010000, 0x80020000))
    assert len(found) == 1 and found[0][2:] == (BASE, 0x20000)


def test_bad_embedded_headers_do_not_turn_random_data_into_overlays():
    data = bytearray(executable())
    struct.pack_into('<I', data, 0x1c, 0x2000000)
    assert overlays.embedded_exes(bytes(data)) == []
    assert overlays.embedded_exes(executable()[:-8]) == []
    assert overlays.embedded_exes(bytes(4) + executable(0x801fff00)) == []


def test_raw_archive_candidates_are_hash_pinned_and_cannot_import_without_review(tmp_path):
    data = bytes(0x20000) + _overlay(BASE, 40)
    image = tmp_path / 'container.bin'
    image.write_bytes(data)
    report = discovery.discover(image)
    assert report['candidates'] >= 1
    parsed = Profile.parse(json.dumps(report['profile']))
    assert all(r.confidence == 'derived' for r in parsed.regions)
    region = parsed.regions[0]
    assert data[region.offset:region.offset + region.size].startswith(struct.pack('<I', 0x27BDFFE8))
    assert region.vram == BASE
    assert 'verify loader' in region.evidence
    assert discovery.discover(image, seconds=0)['truncated']


def test_disc_candidate_keeps_archive_path_offset_and_source_hash(tmp_path):
    archive = bytes(0x20000) + executable()
    image = tmp_path / 'disc.iso'
    image.write_bytes(_iso({'PACK.PAC': archive}))
    report = discovery.discover(image)
    parsed = Profile.parse(json.dumps(report['profile']))
    assert parsed.regions[0].file == 'PACK.PAC'
    assert parsed.regions[0].offset == 0x20800
    assert parsed.regions[0].vram == BASE


def test_scan_and_directory_cycles_are_bounded(monkeypatch):
    class Disc:
        def sector(self, lba):
            pvd = bytearray(2048)
            struct.pack_into('<I', pvd, 158, 18)
            struct.pack_into('<I', pvd, 166, 2048)
            return bytes(pvd)
        def entries(self, lba, length):
            yield 'CYCLE', lba, length, True
            yield 'PACK.PAC', 20, overlays.MAX_ARCHIVE + 1, False
        def read(self, *args):
            raise AssertionError('oversized archive must not be read')
    assert len(overlays.disc_files(Disc())) == 1
    report = {}
    assert overlays.find(Disc(), '', 'psx', (0, 0), report) == []
    assert report['truncated']
    monkeypatch.setattr(discovery.time, 'monotonic', lambda: 100)
    assert discovery.candidates(bytes(0x10000) + _overlay(BASE, 40), deadline=99) == []


def test_cli_discover_writes_new_profile_without_opening_workspace(tmp_path):
    from decompnet.app.mapping_cli import run
    image, output = tmp_path / 'own.bin', tmp_path / 'candidates.json'
    image.write_bytes(executable())
    args = SimpleNamespace(action='discover', image=image, profile=output)
    assert run(args) == 0
    assert Profile.parse(output.read_text()).regions[0].confidence == 'derived'
    assert run(args) == 1  # do not overwrite operator edits


def test_discovered_profile_cannot_change_matching_targets(tmp_path):
    import pytest
    from decompnet.app.engine import RomNetwork
    from decompnet.network import NetworkError
    image = tmp_path / 'archive.bin'
    image.write_bytes(bytes(0x20000) + _overlay(BASE, 40))
    result = discovery.discover(image)
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'workspace')
    with pytest.raises(NetworkError, match='preview-only'):
        net.mapping.create(image, json.dumps(result['profile']), confirmed=True)
    assert net.store.all('SELECT * FROM projects') == []


def test_disc_import_splits_the_exact_embedded_payload(tmp_path):
    import pytest
    from decompnet.app.engine import RomNetwork
    from .test_overlays import EXE
    if not EXE.exists():
        pytest.skip('needs the owned synthetic PS1 demo build')
    disc = _iso({'SYSTEM.CNF': b'BOOT = cdrom:\\BOOT.EXE;1\r\n', 'BOOT.EXE': EXE.read_bytes(),
                 'PACK.PAC': bytes(0x20000) + executable()})
    net = RomNetwork(tmp_path / 'app.db', tmp_path)
    slug = net.import_rom(disc, confirmed=True, log=lambda _: None)
    proj = net._project(slug)
    rows = net.store.all("SELECT * FROM functions WHERE project_id=? AND unit LIKE 'ovl_pack_%'", proj['id'])
    assert len(rows) == 40
    assert min(row['vram'] for row in rows) == BASE
    source = net.cfg(proj)['rom']['overlay_discovery']['sources'][0]
    assert source['file'] == 'PACK.PAC' and source['offset'] == 0x20800


def test_operator_discovery_api_returns_profile_without_project_mutations(tmp_path):
    import time
    from decompnet.app.engine import RomNetwork
    from decompnet.app.server import build
    from starlette.testclient import TestClient
    image = tmp_path / 'archive.bin'
    image.write_bytes(executable())
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'work')
    mcp, _, security = build(net, tmp_path / 'home', 6499, 'test-secret')
    with TestClient(mcp.streamable_http_app(transport_security=security, host='localhost'), base_url='http://localhost:6499') as client:
        payload = {'action': 'discover', 'image': str(image)}
        assert client.post('/api/mapping', json=payload, headers={'X-Decomp': '1'}).status_code == 401
        client.cookies.set('decomp_live', 'test-secret')
        jid = client.post('/api/mapping', json=payload, headers={'X-Decomp': '1'}).json()['job']
        for _ in range(100):
            result = client.get(f'/api/job?id={jid}').json()
            if result['state'] != 'running':
                break
            time.sleep(0.01)
        assert result['state'] == 'done' and result['result']['candidates'] == 1
        assert result['result']['profile']['regions'][0]['confidence'] == 'derived'
    assert net.store.all('SELECT * FROM projects') == []
