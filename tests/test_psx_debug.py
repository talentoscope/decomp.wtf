"""Synthetic debug records: no retail assets or generated game source."""
import json
import struct

import pytest

from decompnet.rom import psyqsym as sym
from .test_psyq import DATA, net_with_overlays

BASE = 0x80010000


def pstr(value):
    value = value.encode()
    return bytes([len(value)]) + value


def record(value, tag, body=b''):
    return struct.pack('<IB', value, tag) + body


def fixture():
    return (b'MND\x01\0\0\0\0'
            + record(BASE, 0x88, struct.pack('<I', 10) + pstr('build/private/unit.c'))
            + record(BASE, 0x94, struct.pack('<HHI', 2, 0x24, 0) + pstr('Tick'))
            + record(BASE, 0x8C, struct.pack('<HIHIiI', 29, 32, 31, 0x80010000, -4, 10)
                     + pstr('build/private/unit.c') + pstr('Tick'))
            + record(4, 0x94, struct.pack('<HHI', 17, 3, 2) + pstr('index'))
            + record(0xfffffff0, 0x94, struct.pack('<HHI', 1, 4, 4) + pstr('counter'))
            + record(BASE, 0x80)
            + record(BASE + 32, 0x8E, struct.pack('<I', 12)))


def test_sym_frame_types_lines_and_paths():
    info = sym.read(fixture())
    fn = info.functions[BASE]
    assert fn['file'] == 'unit.c' and fn['frame_size'] == 32
    assert fn['prototype'] == 'int Tick(short index)'
    assert fn['parameters'][0]['home'] == {'kind': 'register', 'index': 4}
    assert fn['locals'][0]['home']['offset'] == -16
    assert info.lines[0]['line'] == 10
    assert 'private' not in json.dumps(info.__dict__)


@pytest.mark.parametrize('data', [b'MND\x01', fixture()[:-1], b'MND\x01'+b'\0'*4+record(BASE, 0x99), b'CPE\x01\x01'])
def test_invalid_records_fail_without_resynchronizing(data):
    with pytest.raises(sym.DebugFormatError):
        sym.read(data)


def test_derived_declarations_are_conservative():
    assert sym.declaration({'name': 'values', 'type': 0x34, 'dimensions': [8]}) == 'int values[8]'
    assert sym.declaration({'name': 'callback', 'type': 0x94}) == 'int (*callback)()'
    assert sym.declaration({'name': 'opaque', 'type': 8, 'tag': 'Pair'}) == 'struct Pair opaque'
    assert sym.declaration({'name': 'unknown', 'type': 0}) is None


def test_map_cpe_identity_and_overlay_ambiguity():
    files = [('unit.map', b'80010000 Tick\nCount = 80010004;')]
    regions = [('main', DATA, BASE), ('ovl_test', DATA, BASE)]
    assert sym.inspect(files, regions, [])['hits'] == []
    result = sym.inspect(files, regions, [], 'main')
    assert len(result['hits']) == 2
    cpe = b'CPE\x01\x01' + struct.pack('<II', BASE, len(DATA)) + DATA + b'\x03' + struct.pack('<HI', 0x90, BASE) + b'\0'
    assert sym.read(cpe).entry == BASE
    assert sym.read(b'CPE\x01\x02' + struct.pack('<I', BASE) + b'\0').entry is None
    assert sym.inspect(files + [('unit.cpe', cpe)], regions, [], 'main')['checked_bytes'] == len(DATA)
    with pytest.raises(sym.DebugFormatError, match='differs'):
        sym.inspect([('unit.cpe', cpe)], [('main', b'!' * 32, BASE)], [])


def test_preview_aliases_preserve_canonical_names_matches_and_sources(tmp_path):
    net = net_with_overlays(tmp_path)
    files = [('unit.sym', fixture())]
    assert net.import_psx_debug('synthetic', files)['skipped'] >= 1  # ambiguous overlay
    result = net.import_psx_debug('synthetic', files, unit='main')
    assert result['applied'] == 0
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched', matched_source='original', best_source='original' WHERE unit='main'")
    net.import_psx_debug('synthetic', files, apply=True, unit='main')
    fn = net._function('func_80010000', 'synthetic')
    assert fn['status'] == 'matched' and fn['matched_source'] == 'original'
    assert json.loads(fn['meta'])['debug']['details']['prototype'] == 'int Tick(short index)'
    assert net.symbols(1).resolve('Tick') == BASE
    assert net._function('Tick', 'synthetic')['id'] == fn['id']
    assert net.symbols(1).by_addr[BASE].kind != 'alias'
    assert net.symbols(1).resolve('func_80010000') == BASE


def test_claims_and_conflicting_names_are_preserved(tmp_path):
    net = net_with_overlays(tmp_path)
    agent, _ = net.local_agent('you')
    fn = net._function('func_80010000', 'synthetic')
    with net.store.tx() as db:
        db.execute('INSERT INTO claims(function_id,agent_id,claimed_at,expires_at) VALUES(?,?,0,0)', (fn['id'], agent.id))
    assert net.import_psx_debug('synthetic', [('unit.sym', fixture())], apply=True, unit='main')['applied'] == 0


def test_disc_discovery_handles_tiny_debug_files(monkeypatch):
    from decompnet.rom import overlays
    monkeypatch.setattr(overlays, 'disc_files', lambda _: [('DIR/UNIT.MAP', 40, 14), ('IMAGE.BIN', 50, 20)])
    class Disc:
        def read(self, lba, size):
            assert lba == 40
            return b'80010000 Tick\n'
    assert sym.discover(Disc()) == [('UNIT.MAP', b'80010000 Tick\n')]
