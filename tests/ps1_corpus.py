"""Helpers and explicit golden updater for the repository's original PS1 fixtures."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import re
from pathlib import Path

from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.compiler import compile_source
from decompnet.elfobj import extract_function
from decompnet.project import compiler_config
from decompnet.psx_gte import TYPES

ROOT = Path(__file__).parent / 'fixtures' / 'ps1'
CORPUS = json.loads((ROOT / 'corpus.json').read_text())
COMPILERS = [t.id for t in toolchains.registry().values() if t.platform == 'PS1']
RECIPES = [(tid, aspsx) for tid in COMPILERS for aspsx in ('', '2.56')]


def available(tid, aspsx):
    return all(toolchains.installed(t) for t in [tid] + (['maspsx', 'binutils-mips'] if aspsx else []))


def snapshot(case: dict, tid: str, aspsx: str) -> dict:
    cfg = compiler_config(Path('.'), {'compiler': {'toolchain': tid, 'flags': CORPUS['flags'], 'aspsx': aspsx}})
    result = compile_source(cfg, TYPES + case['header'], case['source'])
    if not aspsx and tid in case.get('native_unsupported', []):
        errors = re.findall(r"Error: (.+)", result.log)
        if result.ok or len(errors) != 2 or not all("Illegal operands" in e for e in errors) or "lui" not in errors[0] or "addiu" not in errors[1]:
            raise ValueError('known native-label limitation changed; review this case and its explicit expectation')
        return {'status': 'unsupported', 'reason': case['limitation'], 'alternative': 'ASPSX 2.56'}
    if not result.ok:
        raise ValueError(result.log)
    fn = extract_function(result.obj, 'fixture')
    if fn is None:
        raise ValueError('fixture function was not emitted')
    elf = ELFFile(BytesIO(result.obj))
    data = {}
    for section in elf.iter_sections():
        if section.name in ('.data', '.rodata', '.rdata', '.sdata', '.sdata2', '.bss', '.sbss'):
            data[section.name] = {'size': section['sh_size'], 'alignment': section['sh_addralign'],
                                  'bytes': section.data().hex() if section['sh_type'] != 'SHT_NOBITS' else None}
    signature = {'code': fn.code.hex(), 'relocations': [r.to_json() for r in fn.relocs], 'data': data}
    return {'status': 'compiled', 'sha256': hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest(),
            'code_bytes': len(fn.code), 'data_bytes': sum(s['size'] for s in data.values())}


def main():
    parser = argparse.ArgumentParser(description='Regenerate original synthetic PS1 goldens with every pinned recipe')
    parser.add_argument('--write', action='store_true', required=True)
    parser.parse_args()
    if not all(available(*r) for r in RECIPES):
        raise SystemExit('install all pinned PS1 compilers, maspsx and MIPS binutils before updating')
    out = {'version': 1, 'corpus_sha256': hashlib.sha256((ROOT / 'corpus.json').read_bytes()).hexdigest(), 'recipes': {}}
    for tid, aspsx in RECIPES:
        key = tid + '/' + (aspsx or 'native')
        out['recipes'][key] = {case['id']: snapshot(case, tid, aspsx) for case in CORPUS['compiler_cases']}
    (ROOT / 'golden.json').write_text(json.dumps(out, indent=2) + '\n')
    print(f"wrote {len(RECIPES)} recipes × {len(CORPUS['compiler_cases'])} original cases")


if __name__ == '__main__':
    main()
