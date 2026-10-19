"""Exact PS1 BIOS dispatch wrappers; names describe stock BIOS slots, not inferred C prototypes.

Dispatch destinations and API identifiers: https://psx-spx.consoledev.net/kernelbios/
No BIOS image or library signature bytes are distributed.
"""
from __future__ import annotations

import struct

_A = dict(enumerate('open lseek read write close ioctl exit isatty getc putc todigit atof strtoul strtol abs labs '
                    'atoi atol atob setjmp longjmp strcat strncat strcmp strncmp strcpy strncpy strlen index rindex '
                    'strchr strrchr strpbrk strspn strcspn strtok strstr toupper tolower bcopy bzero bcmp memcpy memset '
                    'memmove memcmp memchr rand srand qsort strtod malloc free lsearch bsearch calloc realloc InitHeap'.split()))
_B = dict(enumerate('alloc_kernel_memory free_kernel_memory init_timer get_timer enable_timer_irq disable_timer_irq '
                    'restart_timer DeliverEvent OpenEvent CloseEvent WaitEvent TestEvent EnableEvent DisableEvent '
                    'OpenTh CloseTh ChangeTh'.split()))
_B.update(dict(enumerate('InitPAD2 StartPAD2 StopPAD2 PAD_init2 PAD_dr ReturnFromException ResetEntryInt HookEntryInt'.split(), 0x12)))
_B.update({0x32 + i: _A[i] for i in range(10)})
_B.update({0x4A: 'InitCARD2', 0x4B: 'StartCARD2', 0x4C: 'StopCARD2', 0x4E: '_card_write', 0x4F: '_card_read',
           0x56: 'GetC0Table', 0x57: 'GetB0Table'})
_C = {0: 'EnqueueTimerAndVblankIrqs', 1: 'EnqueueSyscallHandler', 2: 'SysEnqIntRP', 3: 'SysDeqIntRP',
      6: 'ExceptionHandler', 7: 'InstallExceptionHandlers', 8: 'SysInitMemory', 9: 'SysInitKernelVariables',
      0xA: 'ChangeClearRCnt', 0xC: 'InitDefInt', 0xD: 'SetIrqAutoAck', 0x1C: 'AdjustA0Table'}
TABLES = {0xA0: _A, 0xB0: _B, 0xC0: _C}


def _literal(word: int, register: int) -> int | None:
    # addiu/ori reg,zero,imm; no relocation, register source or sign-dependent destination.
    if word >> 16 in ((9 << 10) | register, (13 << 10) | register):
        return word & 0xFFFF
    return None


def recognize(code: bytes) -> dict | None:
    if len(code) % 4 or not 12 <= len(code) <= 32:
        return None
    words = list(struct.unpack(f'<{len(code) // 4}I', code))
    while words and words[-1] == 0:
        words.pop()
    gate = slot = None
    if len(words) == 3 and words[1] == 0x01400008:  # jr t2; slot in its delay slot
        gate, slot = _literal(words[0], 10), _literal(words[2], 9)
    elif len(words) == 3 and words[2] == 0x01400008 and len(code) >= 16:  # explicit nop delay slot
        gate, slot = _literal(words[1], 10), _literal(words[0], 9)
        if gate is None or slot is None:
            gate, slot = _literal(words[0], 10), _literal(words[1], 9)
    if gate not in TABLES or slot is None or slot > 0xFF:
        return None
    api = TABLES[gate].get(slot)
    return {'gate': f'{gate:02X}', 'slot': slot, 'api': api,
            'name': f'bios_{gate:02X}_{slot:02X}' + (f'_{api}' if api else ''),
            'note': 'Exact dispatch wrapper. API label assumes the stock BIOS table; runtime patches can change it. No prototype is inferred.'}
