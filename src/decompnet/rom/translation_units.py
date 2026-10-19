"""Compile and validate proposed PS1 source-file groups before enabling shared export objects."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

from elftools.elf.elffile import ELFFile

from ..compiler import MAX_SOURCE_BYTES
from ..elfobj import _function_symbols
from . import match
from .psyqsym import basename


def plans(rows: list, requested: list[dict] | None = None) -> list[dict]:
    by_name = {r['name']: r for r in rows}
    if requested is None:
        groups = {}
        for row in rows:
            debug = json.loads(row['meta'] or '{}').get('debug', {})
            file = basename(debug.get('details', {}).get('file') or debug.get('file') or '')
            if file.lower().endswith('.c'):
                groups.setdefault((row['unit'], file), []).append(row['name'])
        return [{'name': file, 'unit': unit, 'functions': names, 'evidence': 'debug source basename; historical TU identity is provisional'}
                for (unit, file), names in sorted(groups.items()) if len(names) >= 2][:64]
    if not isinstance(requested, list) or len(requested) > 64:
        raise ValueError('provide at most 64 translation-unit groups')
    out, assigned = [], set()
    for group in requested:
        if not isinstance(group, dict) or not isinstance(group.get('name'), str) or not isinstance(group.get('functions'), list):
            raise ValueError('each translation unit needs a name and function-name list')
        names = group['functions']
        if not 2 <= len(names) <= 128 or any(not isinstance(n, str) or n not in by_name or n in assigned for n in names) or len(set(names)) != len(names):
            raise ValueError('translation-unit members must be 2–128 known functions, assigned once')
        units = {by_name[n]['unit'] for n in names}
        if len(units) != 1:
            raise ValueError('a translation unit must stay within one memory unit')
        assigned.update(names)
        name = basename(group['name'])
        if not name:
            raise ValueError('translation-unit name is empty')
        out.append({'name': name, 'unit': next(iter(units)), 'functions': names,
                    'evidence': 'operator-proposed source group; compile and layout validation required'})
    return out


def native_layout(obj: bytes, members: list[dict], image: bytes) -> dict:
    elf = ELFFile(BytesIO(obj))
    symbols = _function_symbols(elf)
    names = {m['name'] for m in members}
    if set(symbols) != names or len({symbols[n][0] for n in names}) != 1:
        raise ValueError('combined object contains extra/missing functions or separate code sections')
    section = elf.get_section(symbols[next(iter(names))][0])
    if section.name != '.text':
        raise ValueError('combined code is not in the supported .text section')
    bases = {m['vram'] - symbols[m['name']][1] for m in members}
    flat = {m['rom'] - symbols[m['name']][1] for m in members}
    if len(bases) != 1 or len(flat) != 1 or min(symbols[n][1] for n in names) != 0:
        raise ValueError('native function order/alignment does not reproduce the original addresses')
    base, start = next(iter(bases)), next(iter(flat))
    end = max(offset + size for _, offset, size in symbols.values())
    if start < 0 or start + end > len(image):
        raise ValueError('combined text exceeds the imported image')
    pos, padding = 0, []
    for lo, hi in sorted((offset, offset + size) for _, offset, size in symbols.values()):
        if lo < pos:
            raise ValueError('native function extents overlap')
        if lo > pos:
            padding.append({'offset': pos, 'size': lo - pos})
            if section.data()[pos:lo] != image[start + pos:start + lo]:
                raise ValueError('native inter-function padding differs from the image')
        pos = hi
    if section.data()[end:].strip(b'\0'):
        raise ValueError('combined text has unmapped trailing data')
    for relocs in elf.iter_sections():
        header = getattr(relocs, 'header', {})
        if header.get('sh_type') in ('SHT_REL', 'SHT_RELA') and header['sh_info'] == symbols[next(iter(names))][0]:
            for relocation in relocs.iter_relocations():
                if not any(offset <= relocation['r_offset'] < offset + size for _, offset, size in symbols.values()):
                    raise ValueError('combined text has a relocation outside its member functions')
    return {'text_base': base, 'rom': start, 'text_size': end, 'padding': padding,
            'function_order': sorted(names, key=lambda name: symbols[name][1]),
            'tail_padding': section['sh_size'] - end, 'section_alignment': section['sh_addralign'],
            'section_alignments': {s.name: s['sh_addralign'] for s in elf.iter_sections() if s['sh_size']},
            'member_offsets': {n: symbols[n][1] for n in sorted(names)}}


def apply(net, proj, out: Path, image: bytes, functions: list[dict], rows: dict, private: list,
          proposals: list[dict], log=print) -> list[dict]:
    available = {rows[(f['rom'], f['vram'])]['name']: f for f in functions if (f['rom'], f['vram']) in rows}
    reports = []
    for proposal in proposals:
        report = {**proposal, 'status': 'individual functions', 'reason': ''}
        reports.append(report)
        names = proposal['functions']
        if len(names) > 128:
            report['reason'] = 'source group exceeds 128 members'
            continue
        if any(n not in available or available[n].get('kind') != 'c' for n in names):
            report['reason'] = 'not every proposed member has verified C in this export'
            continue
        members = sorted((available[n] for n in names), key=lambda f: f['rom'])
        original = [rows[(f['rom'], f['vram'])] for f in members]
        if any(f['name'] != row['name'] for f, row in zip(members, original)):
            report['reason'] = 'splitter and project names differ; preserve individual objects'
            continue
        headers = {re.match(r'#include "([^"]+)"', (out / f['src']).read_text(encoding='utf-8'))[1] for f in members}
        if len(headers) != 1:
            report['reason'] = 'members use different context headers'
            continue
        include = next(iter(headers))
        header = (out / 'include' / include).read_text(encoding='utf-8')
        # The compiler supplies its trusted GTE pack itself, just as for each original match.
        if header.startswith('#include "psx_gte.h"\n'):
            header = header[len('#include "psx_gte.h"\n'):]
        source = '\n\n'.join(r['matched_source'].strip() for r in original) + '\n'
        if len(source.encode()) > MAX_SOURCE_BYTES:
            report['reason'] = 'combined source exceeds the compile source limit'
            continue
        result = net._compile_function(proj, original[0], header, source)
        if not result.ok:
            report['reason'] = 'combined source does not compile with the unit recipe'
            continue
        try:
            layout = native_layout(result.obj, [{'name': r['name'], 'vram': r['vram'], 'rom': r['rom_offset']} for r in original], image)
        except ValueError as exc:
            report['reason'] = str(exc)
            continue
        placements, problem = {}, ''
        for row in original:
            linked = match.link(result.obj, row['name'], net._rom_target(row),
                                net.symbols(proj['id'], row['unit']), net.reader(proj, row['unit']))
            if not linked.exact:
                problem = 'combined compilation changes function code or referenced data'
                break
            for name, offsets in linked.placements.items():
                for offset, address in offsets.items():
                    placements.setdefault(name, set()).add(address - offset)
        if problem or any(len(values) != 1 for values in placements.values()):
            report['reason'] = problem or 'combined constant/data layout has conflicting placements'
            continue
        label = re.sub(r'[^A-Za-z0-9_]', '_', Path(proposal['name']).stem)[:64] or 'unit'
        digest = hashlib.sha256((proposal['unit'] + '\0' + proposal['name'] + '\0' + ','.join(names)).encode()).hexdigest()[:12]
        unit_dir = re.sub(r'[^A-Za-z0-9_]', '_', proposal['unit']) or 'main'
        src = f'src/units/{unit_dir}/{label}_{digest}.c'
        obj = f'build/{Path(src).with_suffix(".o").as_posix()}'
        (out / src).parent.mkdir(parents=True, exist_ok=True)
        (out / src).write_text(f'#include "{include}"\n\n{source}', encoding='utf-8')
        group_id = f'{unit_dir}_{digest}'
        own = {name: next(iter(values)) for name, values in placements.items()}
        for i, f in enumerate(members):
            f.update({'src_fallback': f['src'], 'obj_fallback': f['obj'],
                      'own_fallback': {sec: addr for ob, sec, addr in private if ob == f['obj']},
                      'src': src, 'obj': obj, 'translation_unit': group_id, 'text_base': layout['text_base'],
                      'text_offset': layout['member_offsets'][f['name']], 'own_owner': i == 0})
        private[:] = [(ob, sec, addr) for ob, sec, addr in private if ob not in {f['obj_fallback'] for f in members}]
        private.extend((obj, name, address) for name, address in own.items())
        report.update({**layout, 'status': 'combined object', 'id': group_id, 'source': src, 'own': own})
        log(f"translation unit {proposal['name']}: {len(members)} functions compile with the original layout")
    return reports
