"""A conservative, compile-scored K&R experiment for PS1 narrow-parameter near misses.

No parameter is widened or removed. K&R spelling often emits identical code; byte scoring decides usefulness.
"""
from __future__ import annotations

import re
import struct

from .compiler import _COMMENTS_AND_STRINGS
from .handwritten import _mips_branch, _mips_uses

_NARROW = {'char', 'signed char', 'unsigned char', 'short', 'short int', 'signed short', 'signed short int',
           'unsigned short', 'unsigned short int', 's8', 'u8', 's16', 'u16'}
_WIDE = {'int', 'signed', 'signed int', 'unsigned', 'unsigned int', 'long', 'long int', 'signed long',
         'signed long int', 'unsigned long', 'unsigned long int', 's32', 'u32'}


def knr_variant(source: str, function: str) -> str | None:
    if not re.fullmatch(r'[A-Za-z_]\w*', function):
        return None
    masked = _COMMENTS_AND_STRINGS.sub(lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), source)
    header = re.search(r'(?m)^[ \t]*(?:(?:static|extern|inline|__inline__)\s+)*'
                       r'(?:(?:unsigned|signed)\s+)?(?:void|char|short|int|long|s8|u8|s16|u16|s32|u32)\b[\s*]*'
                       + re.escape(function) + r'\s*\((?P<params>[^(){};#]*)\)\s*\{', masked)
    if not header:
        return None
    names, declarations, narrow = [], [], False
    for param in header['params'].split(','):
        match = re.fullmatch(r'\s*(.*?)\b([A-Za-z_]\w*)\s*', param, re.S)
        if not match:
            return None
        prefix, name = match[1].strip(), match[2]
        if not prefix or not re.fullmatch(r'[\w\s*]+', prefix) or name in names:
            return None
        words = prefix.replace('*', ' ').split()
        base = ' '.join(w for w in words if w not in ('register', 'const', 'volatile'))
        pointer = '*' in prefix
        if base not in _NARROW | _WIDE and not (pointer and re.fullmatch(r'(?:struct |union )?[A-Za-z_]\w*', base)):
            return None
        narrow |= not pointer and base in _NARROW
        names.append(name)
        declarations.append(prefix + ' ' + name + ';')
    if not narrow or len(names) > 16:
        return None
    opening, body = header.start('params') - 1, header.end() - 1
    return source[:opening] + '(' + ', '.join(names) + ')\n' + '\n'.join(declarations) + '\n' + source[body:]


def extensions(code: bytes) -> int:
    body = code[:128]
    words = struct.unpack(f'<{len(body) // 4}I', body[:len(body) // 4 * 4])
    total, written, stop = 0, set(), len(words)
    for i, word in enumerate(words):
        if i >= stop:
            break
        op, rs, rt = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        if op == 12 and rt != 0 and 4 <= rs <= 7 and rs not in written and word & 0xFFFF in (0xFF, 0xFFFF):
            total += 1
        if op == 0 and word & 63 == 0 and 4 <= rt <= 7 and rt not in written and (word >> 6) & 31 in (16, 24) and i + 1 < stop:
            nxt, dest, shift = words[i + 1], (word >> 11) & 31, (word >> 6) & 31
            if dest != 0 and (nxt >> 11) & 31 != 0 and nxt & 0xFC00003F == 3 and (nxt >> 21) & 31 == 0 and (nxt >> 16) & 31 == dest and (nxt >> 6) & 31 == shift:
                total += 1
        uses = _mips_uses(word)
        if uses is None:
            break
        written.update(uses[1] - {0})
        if _mips_branch(word):
            stop = min(stop, i + 2)
    return total


def targeted(source: str, function: str, toolchain: str, target: bytes, candidate: bytes) -> list[str]:
    from . import toolchains

    tc = toolchains.registry().get(toolchain)
    if tc is None or tc.platform != 'PS1' or extensions(candidate) <= extensions(target):
        return []
    variant = knr_variant(source, function)
    return [variant] if variant is not None and variant != source else []
