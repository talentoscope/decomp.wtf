"""Trusted GTE helpers compile as C across the PS1 toolchains without accepting raw assembly submissions."""

import json
from pathlib import Path
import struct

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.compiler import CompilerConfig, compile_source
from decompnet.elfobj import extract_function
from decompnet.handwritten import mips_reason
from decompnet.project import compiler_config
from decompnet.psx_gte import TYPES, count


def words(*values):
    return struct.pack('<' + 'I' * len(values), *values)


def config(tid='gcc-2.7.2-psx', aspsx=''):
    needed = [tid] + (['maspsx', 'binutils-mips'] if aspsx else [])
    if any(toolchains.installed(t) is None for t in needed):
        pytest.skip('needs pinned PS1 tools')
    return compiler_config(Path('.'), {'compiler': {'toolchain': tid, 'flags': ['-O2', '-G0'], 'aspsx': aspsx}})


@pytest.mark.parametrize('source,header', [
    ('void f(void) { __asm__("nop"); }', ''),
    ('void f(void) {}', '#define anything() __asm__("nop")'),
    ('#define gte_rtps() fake()\nvoid f(void) {}', ''),
    ('#undef DECOMP_PSX_GTE_H\nvoid f(void) {}', ''),
    ('#de\\\nfine gte_rtps() fake()\nvoid f(void) {}', ''),
])
def test_raw_assembly_and_intrinsic_redefinitions_are_rejected(source, header):
    cfg = CompilerConfig(['must-not-run'], psx_gte=True)
    result = compile_source(cfg, header, source)
    assert not result.ok and result.log.startswith('rejected:')


def test_gte_eligibility_keeps_other_rules_and_ps2_behavior():
    code = words(0x48087000, 0, 0xAC880000, 0x03E00008, 0)  # mfc2 t0,$14; nop; sw t0,0(a0); jr ra; nop
    assert mips_reason(code, False, 'psx', allow_gte=True) is None
    assert 'GTE' in mips_reason(code, False, 'psx')
    assert 'VU0' in mips_reason(code, False, 'ps2', allow_gte=True)
    assert 'coprocessor 0' in mips_reason(words(0x40026000, 0x03E00008, 0), False, 'psx', allow_gte=True)
    assert 'runs on' in mips_reason(words(0x4A180001, 0, 0), False, 'psx', allow_gte=True)
    assert 'before setting' in mips_reason(words(0x48880000, 0x03E00008, 0), False, 'psx', allow_gte=True)


@pytest.mark.parametrize('tid', [t.id for t in toolchains.registry().values() if t.platform == 'PS1'])
@pytest.mark.parametrize('aspsx', ['', '2.56'])
def test_load_transform_store_matches_trusted_command_encoding(tid, aspsx):
    sizes = 'typedef char psx_sizes[(sizeof(SVECTOR)==8 && sizeof(VECTOR)==16 && sizeof(MATRIX)==32 && sizeof(RECT)==8) ? 1 : -1];'
    result = compile_source(config(tid, aspsx), TYPES + sizes,
                            'void f(SVECTOR *p, int *out) { gte_ldv0(p); gte_rtps(); gte_stsxy(out); }')
    assert result.ok, result.log
    fn = extract_function(result.obj, 'f')
    opcodes = list(struct.unpack('<' + 'I' * (len(fn.code) // 4), fn.code))
    at = opcodes.index(0x4A180001)
    assert opcodes[at - 2:at] == [0, 0]
    assert count(fn.code) == 4
    assert mips_reason(fn.code, False, 'psx', allow_gte=True) is None


def test_parameter_masks_cannot_emit_an_arbitrary_instruction():
    result = compile_source(config(), '', 'void f(void) { gte_mvmva(-1,99,-1,123,99); }')
    assert result.ok, result.log
    fn = extract_function(result.obj, 'f')
    instructions = struct.unpack('<' + 'I' * (len(fn.code) // 4), fn.code)
    commands = [w for w in instructions if w >> 26 == 0x12]
    assert commands == [0x4A4FE412]


def test_pointer_arguments_are_evaluated_once_and_context_can_be_large():
    header = '/*' + ' context declaration notes ' * 3000 + '*/\n' + TYPES
    source = 'int *next_vector(void); void f(void) { gte_ldv0(next_vector()); }'
    result = compile_source(config(), header, source)
    assert result.ok, result.log
    fn = extract_function(result.obj, 'f')
    assert sum(r.symbol == 'next_vector' for r in fn.relocs) == 1


def test_old_gte_exclusions_reopen_but_libraries_and_review_marks_stay(tmp_path):
    net = RomNetwork(tmp_path / 'gte.db', tmp_path)
    cfg = {'kind': 'rom', 'platform': 'psx', 'compiler': {'toolchain': 'gcc-2.7.2-psx'}}
    code = words(0, 0, 0x4A180001, 0x03E00008, 0)
    reason = 'uses the GTE (coprocessor 2): legacy exclusion'
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,arch,big_endian,root,config) VALUES(1,'synthetic','synthetic','mips',0,?,?)",
                   (str(tmp_path), json.dumps(cfg)))
        for name, extra in [('candidate', {}), ('library', {'kind': 'library'}), ('reviewed', {'review': 'keep aside'})]:
            db.execute("INSERT INTO functions(project_id,name,unit,status,size,target,meta) VALUES(1,?,'main','asm',20,?,?)",
                       (name, code, json.dumps({'asm_code': reason, **extra})))
    net.twins.set_aside_asm(net._project('synthetic'))
    assert net._function('candidate', 'synthetic')['status'] == 'open'
    assert net._function('library', 'synthetic')['status'] == 'asm'
    assert net._function('reviewed', 'synthetic')['status'] == 'asm'


def test_matched_gte_function_survives_a_rebuildable_export(tmp_path):
    from decompnet.rom.export import export_rom

    if toolchains.installed('binutils-mips') is None:
        pytest.skip('needs pinned MIPS binutils')
    built = compile_source(config(), '', 'void f(void) { gte_rtps(); }')
    assert built.ok, built.log
    code = extract_function(built.obj, 'f').code
    import shutil
    import subprocess
    import sys
    from elftools.elf.elffile import ELFFile

    demo = Path(__file__).resolve().parent.parent / 'projects' / 'demo-psx'
    exe, elf = demo / 'build' / 'DEMOQ_PS1.EXE', demo / 'build' / 'demoq_ps1.elf'
    if not exe.exists() or not elf.exists():
        if not shutil.which('ld.lld'):
            pytest.skip('needs ld.lld to build the synthetic fixture')
        subprocess.run([sys.executable, str(demo / 'build.py')], check=True)
    image = bytearray(exe.read_bytes())
    with elf.open('rb') as stream:
        symbols = list(ELFFile(stream).get_section_by_name('.symtab').iter_symbols())
        symbol = next(s for s in symbols if s.name == 'entity_collides')
        address = symbol['st_value']
        size = next(s['st_value'] for s in symbols if s.name == 'game_tick') - address
    assert size >= len(code)
    load = struct.unpack_from('<I', image, 0x18)[0]
    offset = 0x800 + address - load
    image[offset:offset + size] = code + bytes(size - len(code))
    net = RomNetwork(tmp_path / 'export.db', tmp_path)
    slug = net.import_rom(bytes(image), name='synthetic.exe', confirmed=True, log=lambda _: None)
    fn = net.store.one('SELECT * FROM functions WHERE project_id=? AND vram=?', net._project(slug)['id'], address)
    assert fn is not None
    agent, _ = net.local_agent('gte-test')
    result = net.attempt(agent, fn['name'], f"void {fn['name']}(void) {{ gte_rtps(); }}", slug)
    assert result['matched'], result['text']
    result = export_rom(net, slug, tmp_path / 'export', log=lambda _: None)
    assert result['identical'], result['log']
    assert (tmp_path / 'export' / 'include' / 'psx_gte.h').exists()


def test_project_include_cannot_replace_the_trusted_header(tmp_path):
    shadow = tmp_path / 'psx_gte.h'
    shadow.write_text('#define gte_rtps() __asm__(".word 0xdeadc0de")\n')
    cfg = config()
    cfg.include_dirs = (str(tmp_path),)
    result = compile_source(cfg, '', 'void f(void) { gte_rtps(); }')
    assert result.ok, result.log
    fn = extract_function(result.obj, 'f')
    assert words(0x4A180001) in fn.code and words(0xDEADC0DE) not in fn.code
    assert '0xdeadc0de' in shadow.read_text()
