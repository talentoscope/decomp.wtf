"""A unit's build settings follow it through attempts, batches, workers and export configuration."""

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.compiler import CompileResult
from decompnet.network import NetworkError
from decompnet.project import compiler_settings


def fixture_net(tmp_path):
    net = RomNetwork(tmp_path / 'd.db', tmp_path)
    cfg = {'kind': 'rom', 'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G8'],
                                     'units': {'overlay': {'flags': ['-O1', '-G0']}}}}
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,root,config) VALUES(1,'sample','sample',?,?)",
                   (str(tmp_path), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id,version,header,created_at,unit) VALUES(1,1,'',0,'')")
        for name, unit in [('a', 'main'), ('b', 'overlay'), ('c', 'overlay')]:
            db.execute("INSERT INTO functions(project_id,name,unit,size,n_insns,target,relocs,best_score,best_source) "
                       "VALUES(1,?,?,8,2,?,'[]',1,'void f(void) {}')", (name, unit, b'\0' * 8))
    return net


def test_profile_inheritance_and_compiler_defaults():
    cfg = {'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G8'], 'cmd': ['old-command'],
                        'include_dirs': ['include'], 'units': {'new': {'toolchain': 'gcc-2.8.1-psx'},
                                                             'none': {'flags': []}}}}
    assert compiler_settings(cfg, 'main')['flags'] == ['-O2', '-G8']
    new = compiler_settings(cfg, 'new')
    assert new == {'toolchain': 'gcc-2.8.1-psx', 'include_dirs': ['include']}
    assert compiler_settings(cfg, 'none')['flags'] == []
    assert cfg['compiler']['cmd'] == ['old-command']


def test_batch_memo_separates_settings_and_reuses_identical_units(tmp_path, monkeypatch):
    net = fixture_net(tmp_path)
    seen = []

    def compile_now(proj, header, source):
        seen.append(json.loads(proj['config'])['compiler']['flags'])
        return CompileResult(False, None, 'syntax error')

    monkeypatch.setattr(net, '_compile_now', compile_now)
    agent, _ = net.local_agent('unit-test')
    with net.store.tx() as db:
        db.executemany('INSERT INTO claims VALUES(?,?,0,9e12)', [(i, agent.id) for i in (1, 2, 3)])
    net.attempt_many(agent, ['a', 'b', 'c'], 'bad source', 'sample')
    assert seen == [['-O2', '-G8'], ['-O1', '-G0']]


def test_settings_changes_invalidate_scores_and_allow_failed_source_retry(tmp_path, monkeypatch):
    net = fixture_net(tmp_path)
    seen = []

    def compile_now(proj, header, source):
        seen.append(json.loads(proj['config'])['compiler']['flags'])
        return CompileResult(False, None, 'syntax error')

    monkeypatch.setattr(net, '_compile_now', compile_now)
    agent, _ = net.local_agent('unit-test')
    net.attempt(agent, 'b', 'bad source', 'sample')
    net.set_unit_compiler('sample', 'overlay', flags=['-O2', '-G0'])
    assert net._function('a', 'sample')['best_score'] == 1
    assert net._function('b', 'sample')['best_score'] is None
    net.attempt(agent, 'b', 'bad source', 'sample')
    assert seen == [['-O1', '-G0'], ['-O2', '-G0']]
    net.set_compiler('sample', 'gcc-2.8.1-psx', ['-O1', '-G8'])
    assert compiler_settings(net.cfg(net._project('sample')), 'overlay')['flags'] == ['-O2', '-G0']
    net.set_unit_compiler('sample', 'overlay')
    assert compiler_settings(net.cfg(net._project('sample')), 'overlay')['flags'] == ['-O1', '-G8']
    with pytest.raises(NetworkError, match='unknown source unit'):
        net.set_unit_compiler('sample', 'missing', flags=[])


def test_settings_that_break_matched_functions_are_not_saved(tmp_path, monkeypatch):
    net = fixture_net(tmp_path)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched', matched_source='void b(void) {}' WHERE name='b'")
    monkeypatch.setattr(net, '_compile_now', lambda *args: CompileResult(False, None, 'invalid settings'))
    before = net._project('sample')['config']
    with pytest.raises(NetworkError, match='break existing matches'):
        net.set_unit_compiler('sample', 'overlay', flags=['-O0'])
    assert net._project('sample')['config'] == before


def test_donated_job_carries_the_unit_profile(tmp_path):
    from decompnet import jobs

    net = fixture_net(tmp_path)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='asm' WHERE unit='main'")
    agent, _ = net.local_agent('unit-test')
    job = jobs.next_job(net, agent)
    assert job['unit'] == 'overlay' and job['flags'] == ['-O1', '-G0']


@pytest.mark.parametrize('script', ['export_build.py', 'export_gba_build.py'])
def test_export_build_resolves_profiles_for_new_sources(tmp_path, monkeypatch, script):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    from decompnet.rom import export

    cfg = {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G8'],
           'units': {'overlay': {'flags': ['-O1', '-G0']}}}
    functions = [{'name': 'a', 'seg': 'main', 'unit': 'main', 'addr': 0},
                 {'name': 'b', 'seg': 'overlay', 'unit': 'overlay', 'addr': 0}]
    (tmp_path / 'build.json').write_text(json.dumps({'compiler': cfg, 'functions': functions}))
    script_path = tmp_path / 'build.py'
    script_path.write_text(Path(export.__file__).with_name(script).read_text())
    for fn in functions:
        relative = f"src/{fn['seg']}/{fn['name']}.c" if script == 'export_build.py' else f"src/{fn['name']}.c"
        fn['source'] = relative
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"void {fn['name']}(void) {{}}")
    (tmp_path / 'asm').mkdir()
    (tmp_path / 'asm' / 'pieces.json').write_text('[]')
    build = runpy.run_path(str(script_path))
    namespace = build['build_objects'].__globals__
    seen = []

    def config(root, stored, unit=None):
        flags = compiler_settings(stored, unit)['flags']
        seen.append(flags)
        return SimpleNamespace(cmd=['compiler', '{src}', '-o', '{obj}', *flags])

    def run_in(cfg, root, argv, **kwargs):
        (root / kwargs['outputs'][0]).write_bytes(b'object')
        return SimpleNamespace(returncode=0)

    monkeypatch.setitem(namespace, 'compiler_config', config)
    monkeypatch.setitem(namespace, 'run_in', run_in)
    monkeypatch.setitem(namespace, 'trim', lambda path: None)
    monkeypatch.setitem(namespace, 'binutils', lambda tool: tool)
    if script == 'export_build.py':
        build['build_objects']([('c', f['source'], f"build/{f['name']}.o") for f in functions])
    else:
        build['build_objects']()
    assert seen == [['-O2', '-G8'], ['-O1', '-G0']]


def test_real_ps1_compile_uses_the_units_small_data_setting(tmp_path):
    from decompnet import toolchains
    from decompnet.elfobj import extract_function

    if toolchains.installed('gcc-2.7.2-psx') is None:
        pytest.skip('needs the pinned PS1 compiler')
    net = fixture_net(tmp_path)
    code = []
    for name in ('a', 'b'):
        fn = net._function(name, 'sample')
        result = net._compile_function(net._project('sample'), fn, 'int small;',
                                       f'int {name}(void) {{ return small; }}')
        assert result.ok, result.log
        code.append(extract_function(result.obj, name))
    assert len(code[0].code) < len(code[1].code)
    assert code[0].relocs[0].type != code[1].relocs[0].type
