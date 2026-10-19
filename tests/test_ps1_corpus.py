"""Cross-feature regression corpus with compiler goldens and tool-free instruction checks."""
import hashlib
import json
import struct

import pytest

from decompnet.compiler import check_source
from decompnet.rom import psx_bios, psx_traits, small_data
from .ps1_corpus import CORPUS, RECIPES, ROOT, available, snapshot


def test_corpus_provenance_and_golden_matrix_are_complete():
    golden = json.loads((ROOT / 'golden.json').read_text())
    assert CORPUS['version'] == golden['version'] == 1
    assert 'Original synthetic' in CORPUS['provenance']
    assert golden['corpus_sha256'] == hashlib.sha256((ROOT / 'corpus.json').read_bytes()).hexdigest()
    expected = {case['id'] for case in CORPUS['compiler_cases']}
    assert len(expected) == len(CORPUS['compiler_cases']) == 8
    assert set(golden['recipes']) == {tid + '/' + (aspsx or 'native') for tid, aspsx in RECIPES}
    for cases in golden['recipes'].values():
        assert set(cases) == expected
        for c in cases.values():
            assert c['status'] in ('compiled', 'unsupported')
            if c['status'] == 'compiled':
                assert len(c['sha256']) == 64 and c['code_bytes'] > 0
            else:
                assert c['alternative'] == 'ASPSX 2.56'
    for case in CORPUS['compiler_cases']:
        check_source(case['source'])


@pytest.mark.parametrize('tid,aspsx', RECIPES)
def test_original_corpus_against_pinned_compiler_goldens(tid, aspsx):
    if not available(tid, aspsx):
        pytest.skip('needs all tools for this pinned PS1 recipe')
    expected = json.loads((ROOT / 'golden.json').read_text())['recipes'][tid + '/' + (aspsx or 'native')]
    for case in CORPUS['compiler_cases']:
        assert snapshot(case, tid, aspsx) == expected[case['id']], (tid, aspsx, case['id'])


@pytest.mark.parametrize('case', CORPUS['instruction_cases'], ids=lambda c: c['id'])
def test_original_instruction_evidence_without_external_tools(case):
    code = struct.pack('<' + 'I' * len(case['words']), *(int(w, 16) for w in case['words']))
    facts = psx_traits.analyze(code)
    assert {k: facts[k] for k in case['traits']} == case['traits']
    if 'bios_api' in case:
        assert psx_bios.recognize(code)['api'] == case['bios_api']
    if 'memory_kind' in case:
        assert facts['resolved_memory'][0]['kind'] == case['memory_kind']
    if 'gp_accesses' in case:
        result = small_data.analyze([{'name': 'fixture', 'unit': 'resident', 'target': code}], [], 0)
        assert result['units']['resident']['gp_accesses'] == case['gp_accesses']
