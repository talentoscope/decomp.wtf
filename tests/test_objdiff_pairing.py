"""Pairing indices, generated table labels and mapped function literal validation."""
import json
import subprocess

import pytest

from decompnet import toolchains
from decompnet.forks import objdiff


def report():
    return {'left': {'symbols': [
        {'name': 'original', 'kind': 'SYMBOL_FUNCTION', 'size': '8', 'target_symbol': 0,
         'match_percent': 100.0, 'instructions': []},
        {'name': 'jtbl_80010000', 'kind': 'SYMBOL_OBJECT', 'size': '16', 'target_symbol': 2,
         'match_percent': 75.0},
        {'name': 'jtbl_unpaired', 'kind': 'SYMBOL_OBJECT', 'size': '8'},
    ]}, 'right': {'symbols': [
        {'name': 'renamed', 'kind': 'SYMBOL_FUNCTION', 'match_percent': 100.0, 'instructions': []},
        {'name': 'original', 'kind': 'SYMBOL_FUNCTION', 'match_percent': 0},
        {'name': '.Lcompiler_table', 'kind': 'SYMBOL_OBJECT', 'match_percent': 75.0},
    ]}}


def test_function_and_data_use_objdiff_pairings_instead_of_name_guesses(tmp_path, monkeypatch):
    data = report()
    monkeypatch.setattr(objdiff, '_run', lambda *a, **k: json.dumps(data))
    result = objdiff.diff(None, None, 'original', tmp_path)
    assert result.matched and result.unit_scores == {'original': 100.0}
    assert result.data_pairs[0]['base'] == '.Lcompiler_table'
    assert result.data_pairs[0]['match_percent'] == 75.0
    assert result.data_pairs[1]['base'] is None and result.data_pairs[1]['match_percent'] is None
    assert 'jtbl_unpaired -> unpaired' in result.render() and 'not zero-filled' in result.render()


@pytest.mark.parametrize('index', [None, -1, 9999, True, '0'])
def test_invalid_explicit_mapping_never_falls_back_to_equal_name(index):
    assert objdiff._paired({'name': 'original', 'target_symbol': index}, report()['right']) is None


@pytest.mark.skipif(not toolchains.installed('gcc-2.7.2-psx') or not toolchains.installed('objdiff-cli'),
                    reason='needs pinned PS1 GCC and objdiff')
@pytest.mark.parametrize('first,third', [(2, 3), (1, 9)])
def test_native_cli_mapping_still_checks_changed_constants(tmp_path, first, third):
    tc = toolchains.require('gcc-2.7.2-psx')
    for side, name, constant, later in [('target', 'original', 1, 3), ('base', 'renamed', first, third)]:
        assembly = f""".set noreorder
.section .rodata
.globl jtbl_{side}
jtbl_{side}:
.word {constant},2,{later},4
.text
.globl {name}
.type {name},@function
.ent {name}
{name}:
lui $2,%hi(jtbl_{side})
jr $31
lw $2,%lo(jtbl_{side})($2)
.end {name}
.size {name},.-{name}
"""
        source = tmp_path / (side + '.s'); source.write_text(assembly)
        subprocess.run([str(tc / 'as'), '-EL', '-o', str(tmp_path / (side + '.o')), str(source)], check=True)
    (tmp_path / 'objdiff.json').write_text(json.dumps({'units': [{
        'name': 'pair', 'target_path': 'target.o', 'base_path': 'base.o',
        'symbol_mappings': {'original': 'renamed', 'jtbl_target': 'jtbl_base'}}]}))
    d = objdiff.diff(tmp_path / 'target.o', tmp_path / 'base.o', 'original', tmp_path,
                     options=objdiff.judge_options(), project=tmp_path, unit='pair')
    assert d.match_percent == 100.0 and not d.matched
    assert d.literals
    if first != 1:
        assert d.literals[0].theirs != d.literals[0].yours
    else:
        assert 'every table entry' in d.literals[0].describe()
    assert d.data_pairs[0]['base'] == 'jtbl_base' and d.data_pairs[0]['match_percent'] < 100
