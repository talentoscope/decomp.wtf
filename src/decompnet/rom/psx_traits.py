"""Bounded PS1 brief evidence. Entry-block observations are lower bounds, never a C signature."""
from __future__ import annotations

import struct

from ..handwritten import _mips_branch, _mips_uses
from ..psx_gte import count as gte_count

LIMIT = 4096  # instructions
_NAMES = {**{4 + i: f'a{i}' for i in range(4)}, **{16 + i: f's{i}' for i in range(8)}, 28: 'gp', 29: 'sp', 30: 'fp', 31: 'ra'}
_MEMORY = {0x20: 1, 0x21: 2, 0x22: 4, 0x23: 4, 0x24: 1, 0x25: 2, 0x26: 4,
           0x28: 1, 0x29: 2, 0x2A: 4, 0x2B: 4, 0x2E: 4, 0x32: 4, 0x3A: 4}
_LOADS = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26}
_OPS = set(range(0x13)) | set(_MEMORY) | {0x31, 0x39}


def analyze(code: bytes, tables: list[str] = ()) -> dict:
    code = code or b''
    limited = not code or len(code) > LIMIT * 4 or len(code) % 4 != 0
    body = code[:LIMIT * 4]
    words = struct.unpack(f'<{len(body) // 4}I', body[:len(body) // 4 * 4])
    direct = sum(w >> 26 == 3 or (w >> 26 == 1 and (w >> 16) & 31 in (16, 17)) for w in words)
    indirect = sum(w >> 26 == 0 and w & 63 == 9 and (w >> 11) & 31 != 0 for w in words)
    jumps = sum(w >> 26 == 0 and (w & 63 == 8 or (w & 63 == 9 and (w >> 11) & 31 == 0))
                and (w >> 21) & 31 != 31 for w in words)
    constants, stack, written = {0: 0}, {29: 0}, set()
    args, slots, saves, memory = set(), set(), {}, []
    frame, frame_known, stop, examined = 0, True, min(len(words), 256), 0
    for index, word in enumerate(words[:256]):
        if index >= stop:
            break
        examined += 1
        uses = _mips_uses(word) if word >> 26 in _OPS else None
        if uses is None:
            frame_known = False
            break
        reads, writes = uses
        if word >> 26 == 1 and (word >> 16) & 31 in (16, 17):
            writes = writes | {31}
        args.update(r for r in reads if 4 <= r <= 7 and r not in written)
        op, rs, rt, rd = word >> 26, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
        imm = ((word & 0xFFFF) ^ 0x8000) - 0x8000
        if op in _MEMORY:
            if rs in constants:
                address = (constants[rs] + imm) & 0xFFFFFFFF
                physical = address if address < 0x20000000 else address & 0x1FFFFFFF if 0x80000000 <= address < 0xC0000000 else -1
                kind = ('scratchpad' if address < 0xA0000000 and 0x1F800000 <= physical < 0x1F800400 else
                        'I/O range' if 0x1F801000 <= physical < 0x1F802000 else
                        'expansion I/O range' if 0x1F802000 <= physical < 0x1F804000 else
                        'CPU control range' if 0xFFFE0000 <= address < 0xFFFE0200 else None)
                if kind and len(memory) < 16:
                    memory.append({'offset': index * 4, 'address': address, 'width': _MEMORY[op], 'kind': kind})
            if rs in stack:
                offset = stack[rs] + imm
                if op in _LOADS and offset >= 16:
                    slots.add(offset)
                if op == 0x2B and rt in (*range(16, 24), 28, 30, 31) and rt not in written:
                    saves[_NAMES[rt]] = offset
        value = home = None
        destination = rt
        if op == 15:
            value = (word & 0xFFFF) << 16
        elif op in (8, 9, 13):
            if rs in constants:
                value = ((constants[rs] + imm) if op != 13 else constants[rs] | (word & 0xFFFF)) & 0xFFFFFFFF
            if op in (8, 9) and rs in stack:
                home = stack[rs] + imm
        elif op == 0 and word & 63 in (0x21, 0x25) and (rs == 0 or rt == 0):
            source, destination = (rs or rt), rd
            value, home = constants.get(source), stack.get(source)
        for register in writes - {0}:
            constants.pop(register, None)
            stack.pop(register, None)
        if value is not None and destination:
            constants[destination] = value
        if home is not None and destination:
            stack[destination] = home
        if 29 in writes:
            if 29 not in stack:
                frame_known = False
            else:
                frame = max(frame, -stack[29])
        written.update(writes - {0})
        if _mips_branch(word):
            stop = min(stop, index + 2)  # include the delay slot, then stop at the first transfer
    return {'limited': limited, 'direct_calls': direct, 'indirect_calls': indirect, 'indirect_jumps': jumps,
            'leaf': None if limited else direct + indirect == 0, 'gte_instructions': gte_count(body),
            'entry_instructions': examined, 'frame_bytes': frame if frame_known else None, 'saved_registers': saves,
            'argument_registers': [_NAMES[r] for r in sorted(args)], 'potential_stack_slots': sorted(slots)[:16],
            'resolved_memory': memory, 'jump_tables': sorted(set(tables))[:16],
            'argument_confidence': 'entry-block read-before-write evidence only; forwarded, unused and later arguments may be missing',
            'table_method': 'named symbol/address reference; table bounds and dispatch relationship are not proven' if tables else
                            'no named table reference; indirect jumps alone do not establish a jump table'}


def brief(evidence: dict) -> list[str]:
    e = evidence
    leaf = 'unknown (analysis limited)' if e['leaf'] is None else 'no link calls observed' if e['leaf'] else 'link calls observed'
    frame = str(e['frame_bytes']) + ' bytes' if e['frame_bytes'] is not None else 'unresolved'
    saves = ', '.join(f'{name}@entry_sp{offset:+d}' for name, offset in sorted(e['saved_registers'].items())) or 'none observed'
    lines = ['', '### PS1 instruction evidence',
             f"Calls: {e['direct_calls']} direct, {e['indirect_calls']} indirect; {leaf}. GTE instructions: {e['gte_instructions']}.",
             f'Entry frame: {frame}; saved registers: {saves}.',
             'Argument registers read: ' + (', '.join(e['argument_registers']) or 'none observed') +
             '; potential incoming stack slots: ' + (', '.join(map(str, e['potential_stack_slots'])) or 'none observed') + '.',
             'Confidence: ' + e['argument_confidence'] + '.',
             'Jump-table evidence: ' + (', '.join(e['jump_tables']) + '; ' if e['jump_tables'] else '') + e['table_method'] + '.']
    if e['resolved_memory']:
        lines += ['Resolved entry-block memory accesses: ' + ', '.join(
            f"+0x{r['offset']:X}: 0x{r['address']:08X} ({r['width']} bytes, {r['kind']})" for r in e['resolved_memory']) + '.']
    if e['limited']:
        lines.append('Instruction counts are limited to the first 4096 complete instructions; this is partial evidence.')
    return lines
