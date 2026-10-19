"""GP initialization and threshold hypotheses are grounded in instructions, with layout uncertainty preserved."""

import json
from pathlib import Path
import struct

import pytest

from decompnet import toolchains
from decompnet.app.engine import LEGACY_GP_NOTE, RomNetwork
from decompnet.rom import small_data, split


def code(*words):
    return struct.pack('<' + 'I' * len(words), *words)


def test_last_threshold_wins_and_split_options_are_replaced():
    assert small_data.threshold(['-G8', '-G', '4', '-O2']) == 4
    assert small_data.threshold(['-Ggarbage']) is None
    assert small_data.with_threshold(['-O2', '-G', '4', '-fsigned-char'], 8) == ['-O2', '-fsigned-char', '-G8']


def test_gp_accesses_produce_estimated_layout_candidates_without_a_false_upper_bound():
    fn = {'name': 'f', 'unit': 'main', 'target': code(0x8F82FFF0, 0x03E00008, 0)}
    data = [{'name': 'small', 'address': 0x8001FFF0, 'size': 8, 'kind': 'bss'}]
    report = small_data.analyze([fn], data, 0x80020000)
    row = report['units']['main']
    assert row['gp_accesses'] == row['resolved_accesses'] == 1
    assert row['layout_candidates'] == [8, 4096]
    assert row['confidence'] == 'provisional' and row['objects'][0]['estimated_size'] == 8
    assert 'do not prove an upper threshold' in report['note']
    data[0]['size'] = 8192
    row = small_data.analyze([fn], data, 0x80020000)['units']['main']
    assert row['layout_candidates'] == [] and row['default_threshold'] == 8  # an inferred extent cannot force a large -G


def test_header_gp_alone_does_not_select_g8_and_library_code_is_excluded():
    ordinary = {'name': 'f', 'unit': 'main', 'target': code(0x03E00008, 0)}
    library = {'name': 'sdk', 'unit': 'main', 'target': code(0x8F820000, 0x03E00008, 0),
               'meta': json.dumps({'kind': 'library'})}
    row = small_data.analyze([ordinary, library], [], 0x80020000)['units']['main']
    assert row['gp_accesses'] == 0 and row['default_threshold'] == 0 and row['confidence'] == 'inconclusive'


def test_unresolved_gp_still_reports_actual_gp_use():
    fn = {'name': 'f', 'unit': 'main', 'target': code(0x8F820000, 0x03E00008, 0)}
    report = small_data.analyze([fn], [], 0)
    assert not report['gp_known'] and report['units']['main']['gp_accesses'] == 1
    assert report['units']['main']['default_threshold'] == 8


def startup(words):
    image = code(*words)
    return split._gp_from_entry(image, 0x80010000, [split.Segment('main', 0, len(image), 0x80010000)], strict=True)


def test_gp_setup_in_call_delay_slot_and_after_initialization_loop():
    assert startup([0x3C1C8002, 0x0C004100, 0x279CFFF0]) == 0x8001FFF0
    assert startup([0x1500FFFF, 0, 0x3C1C8002, 0x0C004100, 0x279CFFF0]) == 0x8001FFF0


def test_conflicting_paths_and_gp_clobbers_remain_unresolved():
    # One path calls with an unknown GP, another initializes it.
    assert startup([0x14800003, 0, 0x0C004100, 0, 0x3C1C8002, 0x0C004100, 0x279CFFF0]) == 0
    assert startup([0x3C1C8002, 0x0080E021, 0x279CFFF0, 0x0C004100, 0]) == 0
    assert startup([0x03E00008, 0, 0x3C1C8002, 0x279CFFF0]) == 0
    assert startup([0x3C1C8002, 0x0C004100, 0, 0x279CFFF0]) == 0


@pytest.mark.parametrize('flags, expected', [(None, 0), (['-O1', '-G4'], 4), ([], 8)])
def test_import_uses_instruction_evidence_and_respects_explicit_flags(tmp_path, monkeypatch, flags, expected):
    from types import SimpleNamespace

    header = bytearray(0x800)
    header[:8] = b'PS-X EXE'
    struct.pack_into('<4I', header, 0x10, 0x80010000, 0x80020000, 0x80010000, 8)
    lines = [split.AsmLine(0x800, 0x80010000, 0x03E00008, 'jr', '$ra'),
             split.AsmLine(0x804, 0x80010004, 0, 'nop', '')]
    fn = split.RomFunction('func_80010000', 0x80010000, 0x800, 'main', lines, Path(), set())
    monkeypatch.setattr(split, 'split_rom', lambda *a, **kw: SimpleNamespace(
        functions=[fn], data=[], named={}, segments=[split.Segment('main', 0x800, 0x808, 0x80010000)],
        image='', compiler='PSYQ', gp=0x80020000))
    net = RomNetwork(tmp_path / 'import.db', tmp_path)
    slug = net.import_rom(bytes(header) + code(0x03E00008, 0), name='synthetic.exe', flags=flags,
                          confirmed=True, log=lambda _: None)
    cfg = net.cfg(net._project(slug))
    assert small_data.threshold(cfg['compiler']['flags']) == expected
    report = net.small_data_evidence(slug)
    assert report['units']['main']['active_threshold'] == expected
    agent, _ = net.local_agent('test')
    assert f'small data: -G{expected}' in net.get_function('func_80010000', slug, agent)
    with net.store.tx() as db:
        db.execute('UPDATE context_versions SET header=?', (LEGACY_GP_NOTE,))
    assert '8 bytes or less' not in net.get_context(slug)
    net.set_compiler(slug, 'gcc-2.7.2-psx', ['-O2', '-G4'])
    assert '8 bytes or less' not in net._context(net._project(slug)['id'])['header']


def test_missing_exe_header_gp_is_recovered_before_disassembly(tmp_path):
    if toolchains.installed('gcc-2.7.2-psx') is None:
        pytest.skip('needs the owned PS1 demo fixture')
    path = Path(__file__).resolve().parent.parent / 'projects' / 'demo-psx' / 'build' / 'DEMOQ_PS1.EXE'
    if not path.exists():
        pytest.skip('build the PS1 demo fixture first')
    image = bytearray(path.read_bytes())
    gp = struct.unpack_from('<I', image, 0x14)[0]
    struct.pack_into('<I', image, 0x14, 0)
    net = RomNetwork(tmp_path / 'startup.db', tmp_path)
    slug = net.import_rom(bytes(image), name='synthetic.exe', confirmed=True, log=lambda _: None)
    proj = net._project(slug)
    cfg = net.cfg(proj)
    assert cfg['gp'] == gp and cfg['small_data']['gp_source'] == 'entry'
    config = next(Path(proj['root']).glob('*.yaml')).read_text()
    assert f'gp_value: 0x{gp:08X}' in config


def test_generated_gp_comment_migration_preserves_line_sensitive_compiled_code():
    from decompnet.compiler import compile_source
    from decompnet.elfobj import extract_function
    from decompnet.project import compiler_config
    from decompnet.app.engine import GP_NOTE

    if toolchains.installed('gcc-2.7.2-psx') is None:
        pytest.skip('needs pinned PS1 GCC')
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': 'gcc-2.7.2-psx'}})
    outputs = []
    for note in (LEGACY_GP_NOTE, GP_NOTE):
        result = compile_source(cfg, note + '\nenum { context_line = __LINE__ };', 'int f(void) { return context_line; }')
        assert result.ok, result.log
        outputs.append(extract_function(result.obj, 'f').code)
    assert outputs[0] == outputs[1]
