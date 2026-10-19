"""The optional ASPSX path runs the pinned compiler and assembler without shell expansion."""

from pathlib import Path
import subprocess

import pytest

from decompnet import toolchains
from decompnet.compiler import compile_source
from decompnet.elfobj import extract_function
from decompnet.project import compiler_config, compiler_settings
from decompnet.psx_compile import main, validate_version


def require_tools():
    if any(toolchains.installed(t) is None for t in ('gcc-2.7.2-psx', 'maspsx', 'binutils-mips')):
        pytest.skip('needs pinned PS1 GCC, maspsx and MIPS binutils')


def compile_with(version, source, flags=None):
    require_tools()
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': 'gcc-2.7.2-psx', 'aspsx': version,
                                                   'flags': flags or ['-O2', '-G0']}})
    res = compile_source(cfg, '', source)
    assert res.ok, res.log
    return extract_function(res.obj, 'f')


def test_division_guards_agree_with_the_existing_assembler():
    source = 'int f(int a, int b) { return a / b; }'
    native = compile_with('', source)
    for version in ('2.21', '2.56', '2.86'):
        assert compile_with(version, source).code == native.code


def test_aspsx_version_changes_constant_encoding():
    older = compile_with('2.21', 'int f(void) { return 1; }')
    newer = compile_with('2.56', 'int f(void) { return 1; }')
    assert older.code != newer.code


@pytest.mark.parametrize('flags', [['-O2', '-G8'], ['-O2', '-G', '8']])
def test_small_data_threshold_reaches_maspsx(flags):
    small = compile_with('2.56', 'int g; int f(void) { return g; }', flags)
    large = compile_with('2.56', 'int g; int f(void) { return g; }', ['-O2', '-G0'])
    assert len(small.code) < len(large.code)


def test_failed_compile_stops_before_assembly(monkeypatch, tmp_path):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr('decompnet.psx_compile.subprocess.run', run)
    assert main(['--maspsx', 'maspsx.py', '--assembler', 'as', '--aspsx-version', '2.56', '--',
                 'gcc', '-c', 'source with spaces.c', '-o', str(tmp_path / 'output with spaces.o')]) == 7
    assert len(seen) == 1 and 'source with spaces.c' in seen[0] and '-S' in seen[0]


def test_native_assembler_can_override_a_project_aspsx_profile():
    cfg = {'compiler': {'toolchain': 'gcc-2.7.2-psx', 'aspsx': '2.56', 'units': {'native': {'aspsx': ''}}}}
    assert compiler_settings(cfg, 'native')['aspsx'] == ''
    assert compiler_settings(cfg, 'other')['aspsx'] == '2.56'
    with pytest.raises(ValueError):
        validate_version('2.56; unexpected-command')
