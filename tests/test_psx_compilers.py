"""Each PS1 GCC variant is selectable, drafts use the PS1 target, and installed tools produce little-endian MIPS."""

from pathlib import Path

import pytest

from decompnet import scoring, seeds, toolchains
from decompnet.app.engine import COMPILERS, PLATFORM_COMPILERS, SHORT_NAMES
from decompnet.compiler import compile_source
from decompnet.elfobj import extract_function
from decompnet.project import compiler_config

PS1 = [t.id for t in toolchains.registry().values() if t.platform == 'PS1']


def test_compiler_choices_cover_the_supported_ps1_variants():
    expected = {'gcc-2.5.7-psx', 'gcc-2.6.0-psx', 'gcc-2.6.3-psx', 'gcc-2.7.2-psx', 'gcc-2.7.2-cdk',
                'gcc-2.8.0-psx', 'gcc-2.8.1-psx', 'gcc-2.91.66-psx', 'gcc-2.95.2-psx'}
    assert expected == set(PS1) == set(PLATFORM_COMPILERS['psx'])
    assert expected <= COMPILERS.keys() and expected <= SHORT_NAMES.keys()
    assert all(seeds.M2C_TARGETS[tid] == 'mipsel-gcc-c' for tid in expected)


@pytest.mark.parametrize('tid', PS1)
@pytest.mark.parametrize('aspsx', ['', '2.56'])
def test_installed_ps1_compilers_honor_target_and_signedness(tid, aspsx):
    needed = [tid] + (['maspsx', 'binutils-mips'] if aspsx else [])
    if any(toolchains.installed(t) is None for t in needed):
        pytest.skip('needs pinned PS1 toolchain tools')
    for flag, load in [('-funsigned-char', 'lbu'), ('-fsigned-char', 'lb')]:
        cfg = compiler_config(Path('.'), {'compiler': {'toolchain': tid, 'flags': ['-O2', '-G8', flag], 'aspsx': aspsx}})
        res = compile_source(cfg, '', 'char g; int f(void) { return g; }')
        assert res.ok, res.log
        fn = extract_function(res.obj, 'f')
        assert fn is not None and fn.arch == 'mips' and not fn.big_endian
        assert load in [i.mnemonic for i in scoring.disassemble(fn)]
        assert fn.relocs
