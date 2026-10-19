"""Compiler experiments measure whole profiles on equal samples and never record speculative matches."""

import json
from pathlib import Path

import pytest

from decompnet import seeds, toolchains
from decompnet.app.engine import BASE_HEADER, RomNetwork
from decompnet.compiler import CompileResult, compile_source
from decompnet.elfobj import FunctionObj, extract_function
from decompnet.network import NetworkError
from decompnet.project import compiler_config
from decompnet.rom import psx_probe


def test_variations_keep_unrelated_flags_and_remove_split_threshold():
    profile = {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G', '8', '-fno-builtin'], 'aspsx': ''}
    variants = psx_probe.variations(profile, True)
    assert all('-fno-builtin' in v['flags'] for v in variants)
    assert any(v['flags'] == ['-O2', '-G0', '-fno-builtin'] for v in variants)
    assert any(v['aspsx'] == '2.56' for v in variants)
    assert profile['flags'] == ['-O2', '-G', '8', '-fno-builtin']


def test_rank_prioritizes_linked_matches_and_reports_ties():
    good = {'toolchain': 'a', 'flags': [], 'aspsx': '', 'compiled': 4, 'exact': 3, 'percent': 80}
    pretty = good | {'toolchain': 'b', 'exact': 0, 'percent': 99.9}
    assert psx_probe.rank([pretty, good])[0] == good
    assert psx_probe.confidence([good, good | {'toolchain': 'c'}], 4) == 'tied'
    assert psx_probe.confidence([good], 2) == 'inconclusive'


def make_net(tmp_path, functions):
    net = RomNetwork(tmp_path / 'probe.db', tmp_path)
    image = b''.join(f.code for f in functions)
    (tmp_path / 'synthetic.bin').write_bytes(image)
    cfg = {'kind': 'rom', 'platform': 'psx', 'big_endian': False, 'gp': 0,
           'rom': {'file': 'synthetic.bin'}, 'segments': [{'name': 'main', 'rom_start': 0, 'rom_end': len(image), 'vram': 0x80010000}],
           'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G0']}}
    header = BASE_HEADER.format(title='synthetic')
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,arch,big_endian,root,config) VALUES(1,'synthetic','synthetic','mips',0,?,?)",
                   (str(tmp_path), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id,version,header,created_at,unit) VALUES(1,1,?,0,'')", (header,))
        offset = 0
        for f in functions:
            db.execute("INSERT INTO functions(project_id,name,unit,size,n_insns,target,relocs,asm,vram,rom_offset) "
                       "VALUES(1,?,'main',?,3,?,'[]',?,?,?)",
                       (f.name, len(f.code), f.code, seeds.gnu_asm(f), 0x80010000 + offset, offset))
            offset += len(f.code)
    return net


def test_link_failure_cannot_become_a_probe_match(tmp_path, monkeypatch):
    fn = FunctionObj('f', 'mips', False, bytes.fromhex('0800e0030000000000000000'), [])
    net = make_net(tmp_path, [fn])
    monkeypatch.setattr(seeds, 'available', lambda: True)
    monkeypatch.setattr(seeds, 'generate_from_asm', lambda *a, **kw: 'int f(void) { return 0; }')
    monkeypatch.setattr(toolchains, 'installed', lambda tid: {'installed': True} if tid == 'gcc-2.7.2-psx' else None)
    monkeypatch.setattr(net, '_compile_function', lambda *a: CompileResult(True, b'object', ''))
    monkeypatch.setattr(net, '_evaluate', lambda *a: (fn, False, ['unresolved data']))
    monkeypatch.setattr(net, '_target_obj', lambda *a: fn)
    report = net.probe_compiler('synthetic', log=lambda _: None)
    assert report['samples'] == ['f']
    assert all(r['exact'] == 0 and r['percent'] < 100 for r in report['results'])
    assert report['confidence'] == 'inconclusive' and report['compiles'] <= 96
    assert net.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 0
    assert net._function('f', 'synthetic')['status'] == 'open'
    with pytest.raises(NetworkError, match='unknown source unit'):
        net.probe_compiler('synthetic', 'missing')


def test_no_drafts_gives_an_inconclusive_report(tmp_path, monkeypatch):
    net = make_net(tmp_path, [])
    monkeypatch.setattr(seeds, 'available', lambda: False)
    report = net.probe_compiler('synthetic', log=lambda _: None)
    assert report['results'] == [] and report['confidence'] == 'inconclusive'


def test_real_drafts_are_compiled_and_linked_without_changing_settings(tmp_path):
    needed = ['gcc-2.7.2-psx', 'maspsx', 'binutils-mips']
    if not seeds.available() or any(toolchains.installed(t) is None for t in needed):
        pytest.skip('needs pinned m2c and PS1 tools')
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G0'], 'aspsx': '2.21'}})
    functions = []
    for name, body in [('f0', 'return x ? 1 : 2;'), ('f1', 'return x > 3 ? x : 3;'), ('f2', 'return (x + 3) * 5;')]:
        res = compile_source(cfg, '', f'int {name}(int x) {{ {body} }}')
        assert res.ok, res.log
        functions.append(extract_function(res.obj, name))
    net = make_net(tmp_path, functions)
    before = net.cfg(net._project('synthetic'))['compiler']
    report = net.probe_compiler('synthetic', log=lambda _: None)
    assert len(report['samples']) == 3 and report['compiles'] <= 96
    assert report['results'][0]['exact'] >= 2
    assert net.cfg(net._project('synthetic'))['compiler'] == before
    assert net.store.one('SELECT COUNT(*) AS n FROM attempts')['n'] == 0
    assert 'return x' not in json.dumps(report)


def test_compile_budget_counts_failures_and_measures_equal_samples(tmp_path, monkeypatch):
    functions = [FunctionObj(f'f{i}', 'mips', False, bytes.fromhex('0800e0030000000000000000'), []) for i in range(4)]
    net = make_net(tmp_path, functions)
    monkeypatch.setattr(seeds, 'available', lambda: True)
    monkeypatch.setattr(seeds, 'generate_from_asm', lambda *a, **kw: 'int f(void) { return 0; }')
    monkeypatch.setattr(toolchains, 'installed', lambda tid: {'installed': True})
    monkeypatch.setattr(net, '_compile_function', lambda *a: CompileResult(False, None, 'compile failed'))
    report = net.probe_compiler('synthetic', log=lambda _: None)
    assert report['truncated'] and report['compiles'] == 96
    assert report['compiles'] == len(report['results']) * len(report['samples'])
    assert all(r['compiled'] == 0 and r['percent'] == 0 for r in report['results'])


def test_import_schedules_the_automatic_probe_after_the_project_exists(tmp_path, monkeypatch):
    import struct
    import threading
    from types import SimpleNamespace

    from decompnet.rom import split

    header = bytearray(0x800)
    header[:8] = b'PS-X EXE'
    struct.pack_into('<4I', header, 0x10, 0x80010000, 0, 0x80010000, 12)
    image = bytes(header) + bytes.fromhex('0800e0030000000000000000')
    net = RomNetwork(tmp_path / 'import.db', tmp_path)
    net.auto_probe_compilers = True
    done = threading.Event()
    seen = []

    def automatic(slug):
        seen.append(net._project(slug)['slug'])
        done.set()

    monkeypatch.setattr(net, '_automatic_compiler_probe', automatic)
    monkeypatch.setattr(seeds, 'available', lambda: True)
    monkeypatch.setattr(toolchains, 'installed', lambda tid: {'installed': True})
    monkeypatch.setattr(split, 'split_rom', lambda *a, **kw: SimpleNamespace(
        functions=[], data=[], named={}, segments=[], image='', compiler='PSYQ', gp=0))
    slug = net.import_rom(image, name='SLUS_006.14', confirmed=True, log=lambda _: None)
    assert done.wait(1) and seen == [slug]


def test_recognized_libraries_are_excluded_even_if_reopened_for_agents(tmp_path, monkeypatch):
    fn = FunctionObj('f', 'mips', False, bytes.fromhex('0800e0030000000000000000'), [])
    net = make_net(tmp_path, [fn])
    with net.store.tx() as db:
        db.execute('UPDATE functions SET meta=?', (json.dumps({'kind': 'library', 'for_agents': True}),))
    monkeypatch.setattr(seeds, 'available', lambda: True)

    def unexpected(*args, **kwargs):
        raise AssertionError('library function used as compiler evidence')

    monkeypatch.setattr(seeds, 'generate_from_asm', unexpected)
    report = net.probe_compiler('synthetic', log=lambda _: None)
    assert report['samples'] == [] and report['confidence'] == 'inconclusive'
