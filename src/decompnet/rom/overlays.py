"""PlayStation 1 and 2 overlays: code a game keeps in files on its disc, besides the main executable.

Most PlayStation games load part of their code from other files when they need it (a battle system, a level's
logic, a minigame): overlays. The community's projects list them by hand with the address each is loaded at, found
in the code that loads them or with an emulator. Here the code says it: every file on the disc is checked for MIPS
code, and its load address is the one that makes its own calls (jal: absolute addresses) land on its own functions'
starts (n64code.py does the same for N64 segments).

- A PS2 overlay that is an ELF program says where it goes: its loadable segments are taken as they are. IOP modules
  (.IRX: the I/O processor's code, relocatable) are left out.
- Several overlays may load at the same address (only one is in memory at a time): each is a segment of its own, and
  function names that repeat get the overlay's name in front (split.py).
"""

from __future__ import annotations

import re
import struct
import time
from pathlib import Path

from .n64code import JR_RA, _frame, _place
from .n64 import RomError

MAX_FILE = 16 << 20
MAX_ARCHIVE = 64 << 20
MAX_SCAN = 128 << 20
JR_RA_LE = struct.pack("<I", JR_RA)
RAM = {"psx": (0x80000000, 0x80200000), "ps2": (0x00080000, 0x02000000)}


def _swap(data: bytes) -> bytes:
    """Little-endian words as big-endian ones, for n64code's search (offsets and addresses stay the same)."""
    n = len(data) // 4
    return struct.pack(f">{n}I", *struct.unpack(f"<{n}I", data[:n * 4]))


def _code_like(data: bytes, least: int = 16) -> bool:
    if data.count(JR_RA_LE) < least:  # quick: most files (graphics, sound, levels) stop here
        return False
    words = struct.unpack(f"<{len(data) // 4}I", data[:len(data) // 4 * 4])
    return sum(w == JR_RA for w in words) >= least and sum(_frame(w) for w in words) >= least


def disc_files(disc) -> list[tuple[str, int, int]]:
    """(path, lba, size) of every file on an ISO 9660 disc."""
    pvd = disc.sector(16)
    root_lba, root_len = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
    pending, seen, out, read = [(root_lba, root_len, "")], set(), [], 0
    while pending:
        lba, length, path = pending.pop()
        if (lba, length) in seen:
            continue
        seen.add((lba, length))
        if lba < 0 or not 0 <= length <= 8 << 20:
            raise RomError("invalid ISO directory extent")
        read += length
        if len(seen) > 1024 or read > 16 << 20 or path.count("/") > 32:
            raise RomError("ISO directory traversal exceeds its limits")
        for name, at, size, is_dir in disc.entries(lba, length):
            if name in (".", ".."):
                continue
            if is_dir:
                pending.append((at, size, path + name + "/"))
            else:
                out.append((path + name, at, size))
            if len(out) + len(pending) > 50000:
                raise RomError("ISO directory has too many entries")
    return out


def _elf_segments(data: bytes) -> list[tuple[int, bytes]] | None:
    """(address, bytes) of an EE ELF program's loadable code; None if it isn't one (an IOP module is left out)."""
    if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
        return None
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    if e_machine != 8 or e_type != 2:  # MIPS, executable (an IRX is 0xFF80: relocatable, for the IOP)
        return None
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    out = []
    for i in range(phnum):
        p_type, p_offset, p_vaddr, _, p_filesz, _, p_flags, _ = struct.unpack_from("<8I", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz and p_flags & 1:  # PT_LOAD, executable
            out.append((p_vaddr, data[p_offset:p_offset + p_filesz]))
    return out


def place(data: bytes, platform: str, known: list[tuple[int, int]]) -> tuple[int, int] | None:
    """(the address a raw overlay file is loaded at, where in it its code starts), or None if it can't be told."""
    lo, hi = RAM.get(platform, RAM["psx"])
    if not data or len(data) > hi - lo:
        return None
    words = struct.unpack(f"<{len(data) // 4}I", data[:len(data) // 4 * 4])
    if sum(_frame(w) for w in words) * len({w for w in words if w >> 26 == 3}) > 2_000_000:
        return None  # avoid an unbounded cross-product in the address-vote search
    swapped = _swap(data)
    seg = _place(swapped, 0, len(swapped), 0, [], known, regions=(RAM.get(platform, RAM["psx"]),))
    if seg is None:
        return None
    start, _, vram, _ = seg
    base = vram - start
    if not lo <= base < base + len(data) <= hi or base % 4:
        return None
    if any(a < base + len(data) and base < b for a, b in known):  # over the resident program: not where it goes
        return None
    return base, start


def embedded_exes(data: bytes) -> list[tuple[int, bytes, int]]:
    """Header-delimited PS-X EXE payloads, including executables inside an otherwise opaque archive."""
    from .psx import read_exe

    found, at, checked = [], data.find(b"PS-X EXE"), 0
    while at >= 0 and len(found) < 32 and checked < 4096:
        checked += 1
        if at % 4 == 0 and at + 0x800 <= len(data):
            size = int.from_bytes(data[at + 0x1C:at + 0x20], "little")
            if 0 < size <= 2 << 20 and size % 4 == 0 and at + 0x800 + size <= len(data):
                try:
                    exe, info = read_exe(data[at:at + 0x800 + size])
                    if info["load"] % 4 == 0 and info["load"] + size <= RAM["psx"][1] and _code_like(exe[0x800:], 4):
                        found.append((at + 0x800, exe[0x800:], info["load"]))
                        at = data.find(b"PS-X EXE", at + 0x800 + size)
                        continue
                except (RomError, struct.error):
                    pass
        at = data.find(b"PS-X EXE", at + 4)
    return found


def find(disc, boot: str, platform: str, main: tuple[int, int], report: dict | None = None) -> list[tuple[str, bytes, int, int]]:
    """(name, bytes, load address, where its code starts) of the overlays on a PlayStation 1 or 2 disc. `main` is
    the resident program's address range."""
    out = []
    names: set[str] = set()
    deadline = time.monotonic() + 20
    report = report if report is not None else {}
    report.update({"sources": [], "scanned_bytes": 0, "truncated": False})
    boot_name = boot.upper().replace("\\", "/").rsplit("/", 1)[-1]
    for path, lba, size in disc_files(disc):
        base_name = path.rsplit("/", 1)[-1]
        if base_name == boot_name or base_name == "SYSTEM.CNF" or size < 0x400:
            continue
        if size > MAX_ARCHIVE:
            report["truncated"] = True
            continue
        if report["scanned_bytes"] + size > MAX_SCAN or time.monotonic() >= deadline:
            report["truncated"] = True
            break
        data = disc.read(lba, size)
        report["scanned_bytes"] += len(data)
        if len(data) != size:
            continue
        name = re.sub(r"[^A-Za-z0-9_]", "_", Path(base_name).stem).strip("_").lower() or "overlay"
        while name in names:
            name += "_"
        names.add(name)
        if platform == "psx" and (entries := embedded_exes(data)):
            for offset, code, vaddr in entries:
                if any(a < vaddr + len(code) and vaddr < b for a, b in [main]):
                    continue
                label = f"{name}_{offset:X}"
                out.append((label, code, vaddr, 0))
                names.add(label)
                report["sources"].append({"name": label, "file": path, "offset": offset, "size": len(code),
                                          "vram": vaddr, "method": "embedded PS-X EXE header"})
            continue
        if size > MAX_FILE:
            continue  # opaque raw members need reviewed bounds; mapping discover emits candidates
        if platform == "ps2" and (segs := _elf_segments(data)) is not None:
            for k, (vaddr, code) in enumerate(segs):
                if _code_like(code):
                    out.append((f"{name}{k or ''}", code, vaddr, 0))
                    names.add(f"{name}{k or ''}")
            continue
        if not _code_like(data):
            continue
        placed = place(data, platform, [main])
        if placed is not None:
            out.append((name, data, *placed))
            names.add(name)
            report["sources"].append({"name": name, "file": path, "offset": 0, "size": len(data),
                                      "vram": placed[0], "code_offset": placed[1], "method": "JAL/prologue agreement"})
    return out
