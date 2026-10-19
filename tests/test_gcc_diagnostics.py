"""Owned synthetic C variants through the same sandbox and unit recipes as matching."""
import copy
import json
from pathlib import Path

import pytest

from decompnet import gcc_diagnostics as diag, toolchains
from decompnet.compiler import MAX_DUMP_BYTES, SourceRejected, compile_source
from decompnet.network import NetworkError
from decompnet.project import compiler_config
from .test_mapping import mapped  # noqa: F401
from .test_psx_compilers import PS1


def test_first_difference_uses_pass_order_and_does_not_treat_missing_as_equal():
    r = diag.compare({'rtl': 'same', 'combine': '(reg 3)'}, {'rtl': 'same', 'combine': '(reg 4)'})
    assert r['first_difference'] == 'combine' and '(reg 4)' in r['diff']
    r = diag.compare({'rtl': 'same', 'flow': ''}, {'rtl': 'same'})
    assert r['first_difference'] == 'flow' and r['passes'][1]['state'] == 'unavailable'
    assert diag.normalize('(note 4 3 5 "func.c" 2)') == diag.normalize('(note 4 3 5 "func.c" 99)')
    assert diag.normalize('(reg 2)') != diag.normalize('(reg 3)')


def test_redacted_paths_still_distinguish_path_constants():
    a, b = diag.sanitize('"/private/example-a/file.c"'), diag.sanitize('"/private/example-b/file.c"')
    assert '/private' not in a and a != b
    assert diag.sanitize('"C:\\private\\source.c"').startswith('"<path:')


@pytest.mark.parametrize('tid', PS1 + ['ee-gcc-2.96'])
@pytest.mark.parametrize('aspsx', ['', '2.56'])
def test_all_pinned_ps1_recipes_capture_dumps_without_changing_code(tid, aspsx):
    if tid == 'ee-gcc-2.96' and aspsx:
        pytest.skip('ASPSX applies only to PS1 GCC')
    needed = [tid] + (['maspsx', 'binutils-mips'] if aspsx else [])
    if any(not toolchains.installed(t) for t in needed):
        pytest.skip('needs pinned PS1 toolchain')
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': tid, 'flags': ['-O2', '-G0'], 'aspsx': aspsx}})
    source = 'int f(int x) { return x * 3 + 1; }'
    plain = compile_source(cfg, '', source)
    result = compile_source(cfg, '', source, diagnostics=True)
    from decompnet.elfobj import extract_function
    assert result.ok and (result.artifacts.get('rtl') or result.artifacts.get('00.rtl')), result.log
    assert extract_function(plain.obj, 'f').code == extract_function(result.obj, 'f').code
    assert diag.compare(result.artifacts, result.artifacts)['first_difference'] is None


def test_engine_uses_actual_unit_recipe_and_never_changes_targets(mapped):  # noqa: F811
    if not toolchains.installed('gcc-2.7.2-psx'):
        pytest.skip('needs pinned PS1 GCC')
    net, slug, _, _ = mapped
    proj = net._project(slug)
    fn = net.store.one('SELECT * FROM functions WHERE project_id=?', proj['id'])
    cfg = net.cfg(proj)
    cfg['compiler']['units'] = {fn['unit']: {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O1', '-G0']}}
    with net.store.tx() as db:
        db.execute('UPDATE projects SET config=? WHERE id=?', (json.dumps(cfg), proj['id']))
    before = dict(net._function(fn['name'], slug))
    a = 'int ' + fn['name'] + '(int a, int b) { return a+b; }'
    b = a.replace('a+b', 'a-b')
    result = net.explain_difference(slug, fn['name'], a, b)
    assert result['first_difference'] == 'rtl' and result['flags'] == '["-O1", "-G0"]'
    assert result['unit'] == fn['unit'] and dict(net._function(fn['name'], slug)) == before
    root = Path(result['path'])
    assert (root / 'attempt-a/rtl.rtl').is_file() and (root / '.gitignore').read_text() == '*\n'
    assert '/tmp/' not in (root / 'report.json').read_text()
    assert net.cfg(net._project(slug)) == cfg
    with pytest.raises(SourceRejected):
        net.explain_difference(slug, fn['name'], '__asm__("nop");', a)
    with pytest.raises(NetworkError, match='define'):
        net.explain_difference(slug, fn['name'], 'int other(void) { return 1; }', a)


def test_capture_limit_and_wsl_output_allowlist(tmp_path, monkeypatch):
    from decompnet import compiler
    from decompnet.compiler import CompilerConfig
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(compiler, '_sandbox_mode', lambda _: 'wsl')
    monkeypatch.setattr(compiler, '_root', lambda *a: '/work')
    def fake(cfg, work, argv, timeout, **kwargs):
        seen.update(kwargs)
        (work / 'func.o').write_bytes(b'ELF')
        (work / 'func.c.rtl').write_text('x' * (MAX_DUMP_BYTES + 1))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(compiler, '_exec', fake)
    cfg = CompilerConfig(['gcc', '-c', '{src}', '-o', '{obj}'])
    result = compile_source(cfg, '', 'int f(void) { return 1; }', diagnostics=True)
    assert not result.ok and '4 MiB' in result.log
    assert 'func.c.rtl' in seen['outputs'] and 'func.c.dbr' in seen['outputs']


def test_custom_recipe_is_rejected_without_running_it(mapped):  # noqa: F811
    net, slug, _, _ = mapped
    proj = net._project(slug); cfg = copy.deepcopy(net.cfg(proj))
    cfg['compiler']['cmd'] = ['untrusted-custom-command']
    with net.store.tx() as db:
        db.execute('UPDATE projects SET config=? WHERE id=?', (json.dumps(cfg), proj['id']))
    fn = net.store.one('SELECT name FROM functions WHERE project_id=?', proj['id'])['name']
    with pytest.raises(NetworkError, match='custom command'):
        net.explain_difference(slug, fn, 'int f(void) { return 0; }', 'int f(void) { return 1; }')


def test_operator_route_and_agent_handler_preserve_readonly_scope(mapped, tmp_path, monkeypatch):  # noqa: F811
    import time
    from decompnet.app import tools
    from decompnet.app.server import build
    from starlette.testclient import TestClient
    net, slug, _, _ = mapped
    calls = []
    monkeypatch.setattr(type(net), 'explain_difference', lambda self, *args: calls.append(args) or {'first_difference': 'rtl'})
    mcp, _, security = build(net, tmp_path / 'home', 6499, 'test-secret')
    with TestClient(mcp.streamable_http_app(transport_security=security, host='localhost'), base_url='http://localhost:6499') as client:
        payload = {'slug': slug, 'function': 'f', 'left': 'a', 'right': 'b'}
        assert client.post('/api/compiler/diagnostics', json=payload, headers={'X-Decomp': '1'}).status_code == 401
        client.cookies.set('decomp_live', 'test-secret')
        jid = client.post('/api/compiler/diagnostics', json=payload, headers={'X-Decomp': '1'}).json()['job']
        for _ in range(100):
            result = client.get(f'/api/job?id={jid}').json()
            if result['state'] != 'running':
                break
            time.sleep(0.01)
        assert result['state'] == 'done' and calls == [(slug, 'f', 'a', 'b')]
    agent, _ = net.local_agent('diagnostics')
    name = net.store.one('SELECT name FROM functions WHERE project_id=?', net._project(slug)['id'])['name']
    result = tools.call(net, agent, slug, 'explain_difference', {'function': name, 'left': 'a', 'right': 'b'})
    assert json.loads(result)['first_difference'] == 'rtl'
    assert 'explain_difference' in tools.tool_names(False) and 'explain_difference' not in tools.tool_names(True)
