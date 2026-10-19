"""User-supplied PSY-Q patterns are conservative, unit-scoped and separate from compiler identification."""

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import psyq

DATA = bytes(range(32))


def dataset(name='MemoryCopy', data=DATA):
    return json.dumps([{'name': 'synthetic.OBJ', 'sig': data.hex(' '), 'labels': [{'name': name, 'offset': 0}]}])


def test_wildcards_keep_fixed_evidence_and_leading_underscore_labels():
    record = json.loads(dataset('_memory_copy'))
    tokens = record[0]['sig'].split()
    tokens[8:12] = ['??'] * 4
    record[0]['sig'] = ' '.join(tokens)
    patterns, skipped = psyq.parse(json.dumps(record), '440')
    actual = DATA[:8] + b'ABCD' + DATA[12:]
    report = psyq.scan([('main', actual, 0x80010000, 0x800)], patterns)
    assert not skipped and report['hits'][0]['name'] == '_memory_copy'
    assert report['hits'][0]['rom_offset'] == 0x800
    assert report['toolchain_hints'] == [{'sdk': '440', 'toolchains': ['gcc-2.8.1-psx'], 'aspsx': '2.79'}]


def test_repeated_hits_are_rejected_within_a_unit_but_overlays_are_independent():
    patterns, _ = psyq.parse(dataset(), '400')
    assert psyq.scan([('main', DATA * 2, 0x80010000, 0)], patterns)['hits'] == []
    report = psyq.scan([('a', DATA, 0x80010000, 0), ('b', DATA, 0x80010000, 32)], patterns)
    assert len(report['hits']) == 2 and report['versions'][0]['symbols'] == 2


def test_conflicting_names_and_sdk_ties_are_reported():
    first, _ = psyq.parse(dataset('First'), '400')
    second, _ = psyq.parse(dataset('Second'), '440')
    report = psyq.scan([('main', DATA, 0x80010000, 0)], first + second)
    assert not report['hits'] and report['conflicts'][0]['names'] == ['First', 'Second']
    assert report['best_sdk_versions'] == ['400', '440']


def test_short_and_low_information_patterns_are_skipped():
    text = json.dumps([{'sig': '00 ' * 16, 'labels': [{'name': 'short', 'offset': 0}]},
                       {'sig': '?? ' * 32, 'labels': [{'name': 'wild', 'offset': 0}]}])
    assert psyq.parse(text) == ([], 2)
    with pytest.raises(ValueError, match='hexadecimal'):
        psyq.parse(json.dumps([{'sig': 'arbitrary regex', 'labels': []}]))
    with pytest.raises(ValueError):
        psyq.parse(dataset(), '../version')


def test_timeout_does_not_turn_a_partial_search_into_a_unique_hit():
    patterns, _ = psyq.parse(dataset())
    report = psyq.scan([('main', DATA, 0x80010000, 0)], patterns, seconds=0)
    assert report['truncated'] and not report['hits']


def net_with_overlays(tmp_path):
    net = RomNetwork(tmp_path / 'psyq.db', tmp_path)
    (tmp_path / 'synthetic.bin').write_bytes(DATA * 2)
    cfg = {'kind': 'rom', 'platform': 'psx', 'big_endian': False, 'rom': {'file': 'synthetic.bin'},
           'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G0']},
           'segments': [{'name': 'main', 'rom_start': 0, 'rom_end': 32, 'vram': 0x80010000},
                        {'name': 'ovl_test', 'rom_start': 32, 'rom_end': 64, 'vram': 0x80010000}]}
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,arch,big_endian,root,config) VALUES(1,'synthetic','synthetic','mips',0,?,?)",
                   (str(tmp_path), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id,version,header,created_at,unit) VALUES(1,1,'',0,'')")
        for unit, name, flat in [('main', 'func_80010000', 0), ('ovl_test', 'ovl_test_func_80010000', 32)]:
            db.execute("INSERT INTO functions(project_id,name,unit,size,n_insns,target,relocs,vram,rom_offset) "
                       "VALUES(1,?,?,32,8,?,'[]',?,?)", (name, unit, DATA, 0x80010000, flat))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(1,?,?,32,'func')", (name, 0x80010000))
    return net


def test_preview_then_apply_names_and_sets_aside_each_overlay(tmp_path):
    net = net_with_overlays(tmp_path)
    files = [{'version': '400', 'text': dataset()}]
    preview = net.identify_psyq_libraries('synthetic', files, log=lambda _: None)
    assert preview['label_count'] == 2 and preview['applied'] == 0
    assert net._function('func_80010000', 'synthetic')['status'] == 'open'
    report = net.identify_psyq_libraries('synthetic', apply=True, log=lambda _: None)
    assert report['applied'] == 2 and report['renamed'] == 2
    for name in ['MemoryCopy', 'ovl_test_MemoryCopy']:
        fn = net._function(name, 'synthetic')
        assert fn['status'] == 'asm'
        assert json.loads(fn['meta'])['kind'] == 'library'
    assert net.cfg(net._project('synthetic'))['compiler']['toolchain'] == 'gcc-2.7.2-psx'


def test_active_claims_and_existing_matches_are_preserved(tmp_path):
    net = net_with_overlays(tmp_path)
    agent, _ = net.local_agent('active')
    with net.store.tx() as db:
        db.execute('INSERT INTO claims VALUES(1,?,0,9e12)', (agent.id,))
        db.execute("UPDATE functions SET status='matched', name='already_named', matched_source='source kept' WHERE id=2")
    report = net.identify_psyq_libraries('synthetic', [{'version': '400', 'text': dataset()}], apply=True, log=lambda _: None)
    assert report['applied'] == 1
    assert net._function('func_80010000', 'synthetic')['status'] == 'open'
    matched = net._function('already_named', 'synthetic')
    assert matched['status'] == 'matched' and matched['matched_source'] == 'source kept'


def test_partial_pattern_cannot_set_aside_a_larger_function(tmp_path):
    net = net_with_overlays(tmp_path)
    report = net.identify_psyq_libraries('synthetic', [{'version': '400', 'text': dataset(data=DATA[:24])}],
                                         apply=True, log=lambda _: None)
    assert report['label_count'] == 2 and report['applied'] == 0


def test_a_selected_unit_does_not_change_another_overlay(tmp_path):
    net = net_with_overlays(tmp_path)
    report = net.identify_psyq_libraries('synthetic', [{'version': '400', 'text': dataset()}],
                                         apply=True, unit='main', log=lambda _: None)
    assert report['applied'] == 1 and report['label_count'] == 1
    assert net._function('ovl_test_func_80010000', 'synthetic')['status'] == 'open'
