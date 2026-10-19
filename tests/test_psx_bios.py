"""BIOS classification uses exact original synthetic instruction sequences."""
import json
import struct

import pytest

from decompnet.rom.psx_bios import recognize
from .test_psyq import net_with_overlays


def thunk(gate=0xA0, slot=0x2A):
    return struct.pack('<III', 0x240A0000 | gate, 0x01400008, 0x24090000 | slot)


@pytest.mark.parametrize(('gate', 'slot', 'name'), [(0xA0, 0x2A, 'memcpy'), (0xB0, 8, 'OpenEvent'), (0xC0, 0xA, 'ChangeClearRCnt')])
def test_exact_dispatch_names(gate, slot, name):
    found = recognize(thunk(gate, slot) + bytes(4))
    assert found['api'] == name and found['slot'] == slot
    assert found['gate'] == f'{gate:02X}'
    assert 'No prototype' in found['note']


def test_alternate_literal_order_and_ori_are_supported():
    assert recognize(struct.pack('<IIII', 0x34090008, 0x340A00B0, 0x01400008, 0))['api'] == 'OpenEvent'
    assert recognize(struct.pack('<IIII', 0x240A00B0, 0x24090008, 0x01400008, 0))['api'] == 'OpenEvent'


@pytest.mark.parametrize('code', [thunk()[:-1], thunk() + struct.pack('<I', 0x03E00008),
                                 thunk(0xD0), thunk(slot=0x100),
                                 struct.pack('<III', 0x248A00A0, 0x01400008, 0x2409002A),
                                 struct.pack('<III', 0x240A00A0, 0x01200008, 0x2409002A),
                                 struct.pack('<III', 0x240A00A0, 0x01400008, 0x2489002A),
                                 struct.pack('<III', 0x240A00A0, 0x2409002A, 0x01400008)])
def test_near_misses_are_not_library_matches(code):
    assert recognize(code) is None


def test_unknown_slot_keeps_numeric_name():
    found = recognize(thunk(slot=0xFF))
    assert found['name'] == 'bios_A0_FF' and found['api'] is None


def prepared(tmp_path):
    net = net_with_overlays(tmp_path)
    with net.store.tx() as db:
        db.execute('UPDATE functions SET target=?, size=12, n_insns=3', (thunk(),))
    return net


def test_overlays_get_distinct_names_without_losing_original_symbols(tmp_path):
    net = prepared(tmp_path)
    result = net.identify_psx_bios('synthetic')
    assert result == {'recognized': 2, 'renamed': 2, 'set_aside': 2}
    rows = net.store.all('SELECT * FROM functions')
    assert len({f['name'] for f in rows}) == 2
    for fn in rows:
        assert fn['status'] == 'asm' and json.loads(fn['meta'])['kind'] == 'library'
        bios = json.loads(fn['meta'])['bios']
        assert net._function(bios['original_name'], 'synthetic')['id'] == fn['id']
        assert net.symbols(1).resolve(bios['original_name']) == fn['vram']
        assert net._function(bios['alias'], 'synthetic')['id'] == fn['id']
    assert net.identify_psx_bios('synthetic')['recognized'] == 0


@pytest.mark.parametrize('protection', ['matched', 'claim', 'custom', 'for_agents', 'best_source'])
def test_existing_work_and_user_decisions_are_preserved(tmp_path, protection):
    net = prepared(tmp_path)
    agent, _ = net.local_agent('you')
    original = net._function('func_80010000', 'synthetic')
    with net.store.tx() as db:
        if protection == 'claim':
            db.execute('INSERT INTO claims VALUES(?,?,0,0)', (original['id'], agent.id))
        elif protection == 'matched':
            db.execute("UPDATE functions SET status='matched', matched_source='existing C' WHERE id=?", (original['id'],))
        elif protection == 'custom':
            db.execute("UPDATE functions SET name='UserWrapper' WHERE id=?", (original['id'],))
        elif protection == 'for_agents':
            db.execute('UPDATE functions SET meta=? WHERE id=?', (json.dumps({'for_agents': True}), original['id']))
        else:
            db.execute("UPDATE functions SET best_source='existing draft' WHERE id=?", (original['id'],))
    net.identify_psx_bios('synthetic')
    fn = net.store.one('SELECT * FROM functions WHERE id=?', original['id'])
    assert fn['name'] == ('UserWrapper' if protection == 'custom' else original['name'])
    if protection in ('claim', 'for_agents'):
        assert fn['status'] == 'open' and 'bios' not in json.loads(fn['meta'] or '{}')
    if protection == 'matched':
        assert fn['status'] == 'matched' and fn['matched_source'] == 'existing C'
    if protection == 'best_source':
        assert fn['best_source'] == 'existing draft'
