"""Bounded two-attempt GCC 2.x RTL comparison, without changing matches or compiler settings."""
from __future__ import annotations

from dataclasses import replace
import difflib
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
import time

from .compiler import DUMP_PASSES, check_source, compile_source
from .elfobj import extract_function
from .network import NetworkError
from .project import compiler_config, compiler_settings

_SLOTS = threading.BoundedSemaphore(2)


def sanitize(text: str) -> str:
    """Redact absolute quoted paths without conflating distinct path-valued constants."""
    return re.sub(r'"(?:[A-Za-z]:[\\/]|/)[^"\n]*"',
                  lambda m: '"<path:' + hashlib.sha256(m[0].encode()).hexdigest()[:16] + '>"', text)


def normalize(text: str) -> str:
    # Source line notes are debug locations, not machine operations. Preserve RTL IDs, registers and constants.
    text = re.sub(r'(\(note\s+\d+\s+\d+\s+\d+\s+"[^"\n]*"\s+)\d+(\))', r'\1<line>\2', text)
    return '\n'.join(line.rstrip() for line in sanitize(text).splitlines()).strip() + '\n'


def compare(left: dict[str, str], right: dict[str, str]) -> dict:
    rows, first, excerpt = [], None, ''
    names = set(left) | set(right)
    order = sorted(names) if names and all(re.match(r"^\d\d\.", n) for n in names) else DUMP_PASSES
    for name in order:
        if name not in left and name not in right:
            continue
        a = normalize(left[name]) if name in left else None
        b = normalize(right[name]) if name in right else None
        state = 'equal' if a == b else 'different' if a is not None and b is not None else 'unavailable'
        rows.append({'pass': name, 'state': state,
                     'left_sha256': hashlib.sha256(a.encode()).hexdigest() if a is not None else None,
                     'right_sha256': hashlib.sha256(b.encode()).hexdigest() if b is not None else None})
        if first is None and state != 'equal':
            first = name
            excerpt = '\n'.join(list(difflib.unified_diff(
                (a or '').splitlines(), (b or '').splitlines(), fromfile='attempt-a', tofile='attempt-b', n=2))[:70])[:6000]
    return {'first_difference': first, 'passes': rows, 'diff': excerpt,
            'interpretation': 'First observed normalized dump difference; RTL IDs and compiler bookkeeping can differ. '
                              'This is not a causal proof or a comparison with the original compiler RTL.'}


def explain(net, slug: str, function: str, left: str, right: str) -> dict:
    if not isinstance(left, str) or not isinstance(right, str):
        raise NetworkError('provide two C source strings')
    check_source(left); check_source(right)
    proj = net._project(slug)
    cfg = net.cfg(proj)
    if cfg.get('kind') == 'fork' or cfg.get('platform') not in ('psx', 'ps2'):
        raise NetworkError('RTL diagnostics require a PS1 or PS2 ROM project with a pinned GCC 2.x recipe')
    if net._link(slug) is not None:
        raise NetworkError('run compiler diagnostics on the project host')
    fn = net._function(function, slug)
    settings = compiler_settings(cfg, fn['unit'])
    if not str(settings.get('toolchain', '')).startswith(('gcc-2.', 'ee-gcc-2.')) or settings.get('cmd'):
        raise NetworkError('RTL diagnostics require a pinned GCC 2.x recipe without a custom command')
    if not _SLOTS.acquire(blocking=False):
        raise NetworkError('compiler diagnostics are busy; try again shortly')
    try:
        deadline = time.monotonic() + 60
        cc = compiler_config(Path(proj['root']), cfg, fn['unit'])
        header = net._context(proj['id'], fn['unit'])['header']
        results = []
        for text in (left, right):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkError('compiler diagnostic time budget exhausted')
            result = compile_source(replace(cc, timeout=min(cc.timeout, 20, remaining)), header, text, diagnostics=True)
            if not result.ok:
                raise NetworkError('diagnostic compile failed: ' + sanitize(result.log)[-2000:])
            if extract_function(result.obj, fn['name']) is None:
                raise NetworkError('both attempts must define the selected function')
            if not (result.artifacts.get('rtl') or result.artifacts.get('00.rtl')):
                raise NetworkError('this compiler did not emit its initial RTL dump')
            results.append(result)
        report = compare(results[0].artifacts, results[1].artifacts)
        report |= {'function': fn['name'], 'unit': fn['unit'], 'toolchain': settings['toolchain'],
                   'aspsx': settings.get('aspsx', ''), 'flags': sanitize(json.dumps(settings.get('flags', []))),
                   'attempt_sha256': [hashlib.sha256(s.encode()).hexdigest() for s in (left, right)]}
        parent = Path(proj['root']) / 'diagnostics'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.rtl-', dir=parent) as tmp:
            root = Path(tmp)
            (root / '.gitignore').write_text('*\n', encoding='utf-8')
            for label, result in zip(('attempt-a', 'attempt-b'), results):
                (root / label).mkdir()
                for name, text in result.artifacts.items():
                    (root / label / (name + '.rtl')).write_text(sanitize(text), encoding='utf-8')
            (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            destination = parent / (root.name.removeprefix('.') + '-report')
            root.rename(destination)
        return {**report, 'path': str(destination)}
    finally:
        _SLOTS.release()
