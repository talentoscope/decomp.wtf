"""Cross-feature cases that must preserve distinct overlay identities and referenced table data."""
import json
from pathlib import Path

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.forks import objdiff
from decompnet.rom.export import _symbol_lines
from .test_overlays import EXE, _iso, _overlay
from .test_psyq import net_with_overlays


def test_export_symbol_hints_do_not_collapse_overlapping_memory_units(tmp_path):
    net = net_with_overlays(tmp_path)
    lines = _symbol_lines(net, net._project('synthetic'))
    assert len([s for s in lines if 'type:func' in s]) == 2
    assert any('ovl_test_func_80010000' in s and 'rom:0x20' in s for s in lines)
    assert any('func_80010000 = ' in s and 'rom:0x0' in s for s in lines)


@pytest.mark.parametrize('referenced', [True, False])
def test_later_paired_jump_table_entry_difference_only_blocks_its_user(tmp_path, monkeypatch, referenced):
    row = {'instruction': {'formatted': 'lui v0, %hi(jtbl_target)', 'size': 4,
                           'relocation': {'target_symbol': 1}}} if referenced else {}
    data = {'left': {'symbols': [
        {'name': 'f', 'kind': 'SYMBOL_FUNCTION', 'size': '4', 'target_symbol': 0,
         'match_percent': 100, 'instructions': [row]},
        {'name': 'jtbl_target', 'kind': 'SYMBOL_OBJECT', 'target_symbol': 1, 'size': '16', 'match_percent': 75},
    ]}, 'right': {'symbols': [
        {'name': 'f', 'kind': 'SYMBOL_FUNCTION', 'match_percent': 100, 'instructions': [row]},
        {'name': '.Lcompiler_table', 'kind': 'SYMBOL_OBJECT', 'size': '16', 'match_percent': 75},
    ]}}
    monkeypatch.setattr(objdiff, '_run', lambda *a, **k: json.dumps(data))
    diff = objdiff.diff(None, None, 'f', tmp_path)
    assert diff.matched is not referenced
    if referenced:
        assert 'table' in diff.literals_note()


@pytest.mark.skipif(not EXE.exists() or not toolchains.installed('binutils-mips') or not toolchains.installed('gcc-2.7.2-psx'),
                    reason='needs original PS1 demo and pinned MIPS binutils')
def test_distinct_overlays_at_the_same_address_survive_source_and_disc_exports(tmp_path):
    from decompnet.compiler import compile_source
    from decompnet.elfobj import extract_function
    from decompnet.project import compiler_config
    import yaml

    compiled = compile_source(compiler_config(Path('.'), {'compiler': {'toolchain': 'gcc-2.7.2-psx',
                              'flags': ['-O2', '-G0']}}), '', 'extern void callee(void); void fixture(void) { callee(); }')
    template = bytearray(extract_function(compiled.obj, 'fixture').code)
    assert len(template) == 32
    template[8:12] = (0x0C000000 | ((0x80180060 >> 2) & 0x03FFFFFF)).to_bytes(4, 'little')

    def payload(count):
        code = _overlay(0x80180040, count)
        return bytes(64) + bytes(template) + code[32:]

    original = tmp_path / 'disc.iso'
    original.write_bytes(_iso({'SYSTEM.CNF': b'BOOT = cdrom:\\SYNTH.EXE;1\r\n',
                               'SYNTH.EXE': EXE.read_bytes(),
                               'ALPHA.BIN': payload(40),
                               'BETA.BIN': payload(41)}))
    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'work')
    slug = net.import_rom(original, confirmed=True, log=lambda _: None)
    rows = net.store.all('SELECT * FROM functions WHERE project_id=?', net._project(slug)['id'])
    overlay_rows = [r for r in rows if r['unit'].startswith('ovl_')]
    assert len(overlay_rows) == 81 and len({r['name'] for r in overlay_rows}) == 81
    agent, _ = net.local_agent('corpus')
    for unit in sorted({r['unit'] for r in overlay_rows}):
        own = sorted((r for r in overlay_rows if r['unit'] == unit), key=lambda r: r['vram'])
        source = f"extern void {own[1]['name']}(void); void {own[0]['name']}(void) {{ {own[1]['name']}(); }}"
        verdict = net.attempt(agent, own[0]['name'], source, slug)
        assert verdict['matched'], verdict

    # Exercise migration of an older stored splat layout as well as isolation on a new import.
    config_path = next(Path(net._project(slug)['root']).glob('*.yaml'))
    config = yaml.safe_load(config_path.read_text())
    for segment in config['segments']:
        if isinstance(segment, dict) and segment.get('name', '').startswith('ovl_'):
            assert segment.pop('exclusive_ram_id') == segment['name']
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    result = net.export_rom(slug, log=lambda _: None)
    assert result['identical'], result['log']
    assert result['matched'] == 2 and not result['fallback']
    entries = json.loads((Path(result['path']) / 'build.json').read_text())['functions']
    assert {e['name'] for e in entries} >= {r['name'] for r in overlay_rows}
    assert net.export_disc(slug, original, log=lambda _: None)['identical']
