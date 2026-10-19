"""Narrow-parameter retries are scoped, bounded experiments; K&R does not guarantee different code."""
import base64
import json
import random
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet import jobs, narrow_params as narrow, permuter, toolchains
from decompnet.app.permute import State
from decompnet.compiler import CompileResult, compile_source
from decompnet.elfobj import FunctionObj, extract_function
from decompnet.project import compiler_config
from .test_psyq import net_with_overlays


def code(*words):
    return struct.pack('<' + 'I' * len(words), *words)


SIGNED = code(0x00041400, 0x00021403, 0x03E00008, 0x24420001)
WIDE = code(0x03E00008, 0x24820001)
SOURCE = 'int f(short value) { return value + 1; }'


def test_signature_rewrite_preserves_types_and_body():
    source = '/* f(short fake) {} */\nstatic int f(short value, const int *p) { return value + *p; }'
    actual = narrow.knr_variant(source, 'f')
    assert actual == '/* f(short fake) {} */\nstatic int f(value, p)\nshort value;\nconst int * p;\n{ return value + *p; }'
    assert narrow.knr_variant('int f(unsigned short value)\n{ return value; }', 'f')


@pytest.mark.parametrize('source', ['int f(int value) {return value;}', 'int f(short value, float y) {return value;}',
                                  'int f(short value, ...) {return value;}', 'int f(short value, int (*cb)(void)) {return value;}',
                                  'int f(short value, int array[2]) {return value;}',
                                  'int f(short value) __attribute__((noinline)) {return value;}',
                                  'int f(short value, Unknown y) {return value;}',
                                  'int f(value) short value; {return value;}',
                                  'int Other(short value) {return value;}'])
def test_complex_or_unrelated_declarations_are_not_guessed(source):
    assert narrow.knr_variant(source, 'f') is None


def test_extension_evidence_requires_original_argument_value_and_matching_shift_pair():
    assert narrow.extensions(SIGNED) == 1
    assert narrow.extensions(code(0x308200FF, 0x03E00008, 0)) == 1
    assert narrow.extensions(code(0x24040000) + SIGNED) == 0
    assert narrow.extensions(code(0x00041400, 0x00021603, 0x03E00008, 0)) == 0
    assert narrow.extensions(code(0x30820007, 0x03E00008, 0)) == 0


def test_candidates_need_ps1_profile_and_extra_conversion_evidence():
    assert narrow.targeted(SOURCE, 'f', 'gcc-2.7.2-psx', WIDE, SIGNED)
    assert narrow.targeted(SOURCE, 'f', 'gcc-2.7.2-psx', SIGNED, SIGNED) == []
    assert narrow.targeted(SOURCE, 'f', 'gcc-2.7.2-kmc', WIDE, SIGNED) == []
    assert narrow.targeted(SOURCE, 'f', 'unknown', WIDE, SIGNED) == []


@pytest.mark.parametrize('tid', [t.id for t in toolchains.registry().values() if t.platform == 'PS1'])
def test_simple_knr_form_compiles_but_does_not_magically_remove_conversion(tid):
    if not toolchains.installed(tid):
        pytest.skip('needs pinned PS1 compiler')
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': tid, 'flags': ['-O2', '-G0']}})
    functions = []
    for source in [SOURCE, narrow.knr_variant(SOURCE, 'f')]:
        result = compile_source(cfg, '', source)
        assert result.ok, result.log
        functions.append(extract_function(result.obj, 'f'))
    assert functions[0].code == functions[1].code
    assert narrow.extensions(functions[0].code) == 1


def test_donated_search_scores_targeted_retry_before_random_mutations(monkeypatch):
    seen, improvements = [], []
    class Scorer:
        def __init__(self, *args):
            self.target = SimpleNamespace(code=WIDE)
            self.last_candidate = SimpleNamespace(code=SIGNED)
        def score(self, source):
            seen.append(source)
            return 10 if source == SOURCE else 0
    monkeypatch.setattr(permuter, 'LocalScorer', Scorer)
    job = {'source': SOURCE, 'function': 'f', 'toolchain': 'gcc-2.7.2-psx', 'best_score': 10}
    assert permuter.search(job, 1, lambda score, source: improvements.append(source), log=lambda _: None)[0] == 0
    assert seen == [SOURCE, narrow.knr_variant(SOURCE, 'f')] and improvements == seen[1:]


def test_app_uses_same_retry_path_and_existing_score_verification(tmp_path, monkeypatch):
    import decompnet.app.permute as app
    net = net_with_overlays(tmp_path)
    source = SOURCE.replace(' f(', ' func_80010000(')
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=?,best_source=?,best_score=10 WHERE unit='main'", (WIDE, source))
    fn, proj = net._function('func_80010000', 'synthetic'), net._project('synthetic')
    compiled, scored = [], []
    monkeypatch.setattr(net, '_compile_function', lambda p, f, h, s: compiled.append(s) or CompileResult(True, b'object', ''))
    monkeypatch.setattr(app, 'extract_function', lambda *args: FunctionObj(fn['name'], 'mips', False, SIGNED, []))
    monkeypatch.setattr(net.permuter, '_score', lambda a, p, f, s, *args: scored.append(s) or 0)
    agent, _ = net.local_agent('permuter')
    state = State()
    assert net.permuter._climb(agent, proj, fn, state, 1, random.Random(1), threading.Event()) == 0
    assert compiled == [source] and scored == [narrow.knr_variant(source, fn['name'])]
    assert state.variants == 1 and state.matched == 1


def test_worker_rom_jobs_use_canonical_relocations_and_link_verdict(tmp_path, monkeypatch):
    net = net_with_overlays(tmp_path)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_source='int f(void){return 0;}',best_score=10,relocs=? WHERE unit='main'",
                   (json.dumps([[0, 5, 0x80010040]]),))
        db.execute("INSERT INTO symbols VALUES(1,'global_value',2147549248,4,'data',NULL)")
    agent, _ = net.local_agent('worker')
    job = jobs.next_job(net, agent)
    assert len(job['target']['relocs'][0]) == 4 and job['target']['relocs'][0][2] == 'global_value'
    assert base64.b64decode(job['target']['code'])
    fn, proj = net._function(job['function'], 'synthetic'), net._project('synthetic')
    monkeypatch.setattr(net, '_compile_function', lambda *args: CompileResult(True, b'object', ''))
    monkeypatch.setattr(net, '_evaluate', lambda *args: (net._target_obj(fn, proj), False, ['linked data differs']))
    result = jobs.submit(net, agent, 'synthetic', fn['name'], 'candidate C')
    assert result['score'] > 0 and not result['matched']
    assert net._function(fn['name'], 'synthetic')['status'] == 'open'
