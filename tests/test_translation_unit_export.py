"""Source-file grouping is enabled only after code, native offsets and full-image verification."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from decompnet.rom import translation_units as tu
from .test_export import FUNCTIONS, _decompile, _ready


def test_explicit_plans_preserve_memory_unit_identity_and_unique_members():
    rows = [{'name': 'first', 'unit': 'main', 'meta': '{}'}, {'name': 'second', 'unit': 'main', 'meta': '{}'},
            {'name': 'overlay', 'unit': 'ovl', 'meta': '{}'}]
    assert tu.plans(rows, [{'name': 'private/build/unit.c', 'functions': ['first', 'second']}])[0]['name'] == 'unit.c'
    with pytest.raises(ValueError, match='one memory unit'):
        tu.plans(rows, [{'name': 'unit.c', 'functions': ['first', 'overlay']}])
    with pytest.raises(ValueError, match='assigned once'):
        tu.plans(rows, [{'name': 'a.c', 'functions': ['first', 'second']}, {'name': 'b.c', 'functions': ['first', 'second']}])


def test_debug_basenames_are_provisional_and_unit_scoped():
    rows = [{'name': name, 'unit': unit, 'meta': json.dumps({'debug': {'file': 'private/build/unit.c'}})}
            for unit in ('main', 'overlay') for name in (unit + '_a', unit + '_b')]
    groups = tu.plans(rows)
    assert len(groups) == 2 and all(g['name'] == 'unit.c' and 'provisional' in g['evidence'] for g in groups)
    assert set(groups[0]['functions']).isdisjoint(groups[1]['functions'])


def test_native_group_rebuilds_and_clean_clone_still_matches(tmp_path):
    if not _ready('psx'):
        pytest.skip('needs pinned PS1 compiler and binutils')
    net, slug = _decompile(tmp_path, 'psx')
    result = net.export_rom(slug, log=lambda _: None,
                            translation_units=[{'name': 'unit.c', 'functions': FUNCTIONS}])
    assert result['identical'], result['log']
    assert result['translation_units'][0]['status'] == 'combined object', result['translation_units']
    tree = Path(result['path'])
    config = json.loads((tree / 'build.json').read_text())
    grouped = [f for f in config['functions'] if f.get('translation_unit')]
    assert len(grouped) == len(FUNCTIONS) and len({f['c'] for f in grouped}) == 1
    for generated in ('asm', 'assets', 'build'):
        shutil.rmtree(tree / generated, ignore_errors=True)
    proc = subprocess.run([sys.executable, 'build.py'], cwd=tree, capture_output=True, text=True)
    assert proc.returncode == 0 and 'identical' in proc.stdout, proc.stdout + proc.stderr


def test_group_check_protects_other_members_and_missing_group_uses_fallback(tmp_path):
    if not _ready('psx'):
        pytest.skip('needs pinned PS1 compiler and binutils')
    net, slug = _decompile(tmp_path, 'psx')
    for fn in net.store.all('SELECT id,name,meta FROM functions'):
        if fn['name'] in FUNCTIONS:
            meta = json.loads(fn['meta'] or '{}')
            meta['debug'] = {'file': 'unit.c'}
            with net.store.tx() as db:
                db.execute('UPDATE functions SET meta=? WHERE id=?', (json.dumps(meta), fn['id']))
    result = net.export_rom(slug, log=lambda _: None)  # automatic provisional debug-file grouping
    assert result['identical'] and result['translation_units'][0]['status'] == 'combined object'
    tree = Path(result['path'])
    config = json.loads((tree / 'build.json').read_text())
    source = tree / next(f['c'] for f in config['functions'] if f.get('translation_unit'))
    original = source.read_text()
    assert 'return v;' in original
    source.write_text(original.replace('return v;', 'return v + 1;', 1))
    before = (tree / 'build.json').read_bytes()
    check = lambda name: subprocess.run([sys.executable, 'build.py', 'check', name], cwd=tree, capture_output=True, text=True)
    assert check('abs_s32').returncode == 1  # this member was not edited; the entire group is protected
    assert (tree / 'build.json').read_bytes() == before
    source.write_text(original)
    assert check('abs_s32').returncode == 0
    source.unlink()
    rebuilt = subprocess.run([sys.executable, 'build.py'], cwd=tree, capture_output=True, text=True)
    assert rebuilt.returncode == 0 and 'identical' in rebuilt.stdout, rebuilt.stdout + rebuilt.stderr
    assert check('clamp_s32').returncode == 0
    assert not any(f.get('translation_unit') for f in json.loads((tree / 'build.json').read_text())['functions'])


def test_incompatible_native_offsets_keep_verified_individual_export(tmp_path):
    if not _ready('psx'):
        pytest.skip('needs pinned PS1 compiler and binutils')
    net, slug = _decompile(tmp_path, 'psx')
    result = net.export_rom(slug, log=lambda _: None,
                            translation_units=[{'name': 'separate.c', 'functions': ['clamp_s32', 'rng_next']}])
    assert result['identical'], result['log']
    report = result['translation_units'][0]
    assert report['status'] == 'individual functions' and 'order/alignment' in report['reason']


def test_failed_combined_rebuild_restores_individual_sources_and_objdiff(tmp_path, monkeypatch):
    if not _ready('psx'):
        pytest.skip('needs pinned PS1 compiler and binutils')
    from decompnet.rom import export
    net, slug = _decompile(tmp_path, 'psx')
    original_run, builds = subprocess.run, []
    def run(argv, *args, **kwargs):
        if 'build.py' in argv:
            builds.append(argv)
            if len(builds) == 1:
                return subprocess.CompletedProcess(argv, 1, 'synthetic combined-build failure', '')
        return original_run(argv, *args, **kwargs)
    monkeypatch.setattr(export.subprocess, 'run', run)
    result = net.export_rom(slug, log=lambda _: None, translation_units=[{'name': 'unit.c', 'functions': FUNCTIONS}])
    assert result['identical'] and len(builds) == 2
    assert result['translation_units'][0]['status'] == 'individual functions'
    tree = Path(result['path'])
    config = json.loads((tree / 'build.json').read_text())
    assert not any(f.get('translation_unit') for f in config['functions'])
    units = json.loads((tree / 'objdiff.json').read_text())['units']
    assert not any('/units/' in u.get('base_path', '') for u in units)


def test_native_offsets_alignment_and_padding_are_checked_without_tools(monkeypatch):
    from types import SimpleNamespace
    class Section(dict):
        name = '.text'
        raw = b'A' * 8 + bytes(8) + b'B' * 8 + bytes(8)
        def data(self):
            return self.raw
    section = Section(sh_size=32, sh_addralign=16)
    monkeypatch.setattr(tu, 'ELFFile', lambda _: SimpleNamespace(get_section=lambda _: section, iter_sections=lambda: iter([section])))
    monkeypatch.setattr(tu, '_function_symbols', lambda _: {'first': (1, 0, 8), 'second': (1, 16, 8)})
    members = [{'name': 'first', 'vram': 0x80010000, 'rom': 4}, {'name': 'second', 'vram': 0x80010010, 'rom': 20}]
    image = b'HEAD' + section.raw[:24]
    layout = tu.native_layout(b'object', members, image)
    assert layout['padding'] == [{'offset': 8, 'size': 8}] and layout['tail_padding'] == 8
    assert layout['section_alignment'] == 16 and layout['member_offsets']['second'] == 16
    with pytest.raises(ValueError, match='order/alignment'):
        tu.native_layout(b'object', [members[0], {**members[1], 'vram': 0x80010014}], image)
    changed = bytearray(image)
    changed[12] = 1
    with pytest.raises(ValueError, match='padding'):
        tu.native_layout(b'object', members, bytes(changed))
    section.raw = section.raw[:24] + b'X' + bytes(7)
    with pytest.raises(ValueError, match='unmapped trailing'):
        tu.native_layout(b'object', members, image)


def test_conflicting_shared_constant_placements_reject_group(tmp_path, monkeypatch):
    from types import SimpleNamespace
    functions, rows = [], {}
    (tmp_path / 'include').mkdir()
    (tmp_path / 'include' / 'common.h').write_text('')
    for i, name in enumerate(('first', 'second')):
        source = f'src/{name}.c'
        (tmp_path / source).parent.mkdir(exist_ok=True)
        (tmp_path / source).write_text('#include "common.h"\nint ' + name + '(void){return 0;}')
        fn = {'name': name, 'unit': 'main', 'rom': i * 8, 'vram': 0x80010000 + i * 8,
              'kind': 'c', 'src': source, 'obj': f'build/{name}.o'}
        functions.append(fn)
        rows[(fn['rom'], fn['vram'])] = {**fn, 'rom_offset': fn['rom'], 'matched_source': 'int ' + name + '(void){return 0;}'}
    net = SimpleNamespace(_compile_function=lambda *args: SimpleNamespace(ok=True, obj=b'object'),
                          _rom_target=lambda row: row, symbols=lambda *args: None, reader=lambda *args: None)
    monkeypatch.setattr(tu, 'native_layout', lambda *args: {'text_base': 0x80010000, 'member_offsets': {'first': 0, 'second': 8}})
    monkeypatch.setattr(tu.match, 'link', lambda obj, name, *args: SimpleNamespace(
        exact=True, placements={'.rodata': {0: 0x80020000 if name == 'first' else 0x80030000}}))
    report = tu.apply(net, {'id': 1}, tmp_path, bytes(16), functions, rows, [],
                      [{'name': 'unit.c', 'unit': 'main', 'functions': ['first', 'second']}], log=lambda _: None)
    assert report[0]['status'] == 'individual functions' and 'conflicting placements' in report[0]['reason']
    assert all(not f.get('translation_unit') for f in functions)
