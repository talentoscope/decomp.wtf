"""Commodore 64 programs: containers, BASIC, unpackers, and function discovery.

A file arrives as a PRG, T64, D64, TAP, CRT or G64. Each contained file is a block (load address,
bytes, type, name). A PRG's first two bytes are the little-endian load address; a T64 payload is
not, the directory entry carries the address. BASIC V2 is detokenised from the load address
(``c64_basic``). The first SYS is where machine code starts.

From there a 6502 (py65) runs with the 6510 processor port modelled, so a decruncher that banks out
ROM and copies itself to the stack page can finish. A jump to memory just written is the unpacked
program when those bytes are not a copy of the file that was loaded; a jump to a copy of the file
is the loader, and execution continues. If emulation cannot get there (no loader behaviour, an
illegal opcode, a jump into ROM, or the instruction cap), the SYS address is the entry and the
note says so.

The bytes handed to the rest of the app are a ``C64R`` image: a 64-byte header and the 64 KiB RAM
snapshot. A function's file offset is ``64 + cpu address``. Functions are traced from the entry and
from IRQ/NMI/BRK vectors, including ones the program plants at ``$0314`` / ``$0318`` / ``$FFFE``.
Calls into the BASIC or KERNAL ROM (the usual ``$01 = $37`` map) stay named externals.

Hardware and ROM names are applied to the stored listing (``c64_hw``).
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import c64_basic, c64_hw, c64_vice, gb, mos6502
from .n64 import RomError

MAGIC = b"C64R"
HEADER = 64
RAM = 0x10000
MAX_FUNCTION = 0x1000
MAX_STEPS = 3_000_000
EARLY_STEPS = 2_000
RELOC_MIN = 16

_D64_SIZES = {174848: 35, 175531: 35, 196608: 40, 197376: 40}
_SECTORS = [0] + [21] * 17 + [19] * 7 + [18] * 6 + [17] * 5 + [17] * 5  # index 1..40
_GCR = (0x0A, 0x0B, 0x12, 0x13, 0x0E, 0x0F, 0x16, 0x17, 0x09, 0x19, 0x1A, 0x1B, 0x0D, 0x1D, 0x1E, 0x15)
_UNGCR = {n: i for i, n in enumerate(_GCR)}
_BRANCHES = frozenset({"bcc", "bcs", "beq", "bmi", "bne", "bpl", "bvc", "bvs"})
_VECTORS = (0x0314, 0x0315, 0x0316, 0x0317, 0x0318, 0x0319, 0xFFFA, 0xFFFB, 0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF)


@dataclass
class Block:
    """One file pulled out of a container. ``data`` has no PRG header; ``load`` is its CPU address."""

    name: str
    load: int
    data: bytes
    kind: str
    source: str


@dataclass
class Image:
    ram: bytes
    entry: int
    load: int
    load_end: int
    basic_end: int
    sys: int
    method: str
    note: str
    name: str
    basic_text: str = ""
    blocks: list[Block] = field(default_factory=list)
    regs: dict | None = None  # a VICE dump's CPU registers when the RAM was taken (pc, a, x, y, sp, p00, p01)


def sniff(data: bytes, name: str = "") -> str | None:
    """Which container this is, or None. Magics win over the filename; a PRG is recognised by name
    (it has no magic). A D64 is recognised by its size and the BAM signature too."""
    if data.startswith(b"C64 tape image file") or data.startswith(b"C64S tape image file"):
        return "t64"
    if c64_vice.is_vsf(data):
        return "vsf"
    if data.startswith(b"C64-TAPE-RAW"):
        return "tap"
    if data.startswith(b"C64 CARTRIDGE"):
        return "crt"
    if data.startswith(b"GCR-1541"):
        return "g64"
    if data.startswith(b"C64File"):
        return "prg"
    if _d64_bam_ok(data):
        return "d64"
    ext = Path(name).suffix.lower()
    return {".prg": "prg", ".p00": "prg", ".t64": "t64", ".tap": "tap", ".crt": "crt", ".g64": "g64",
            ".d64": "d64", ".vsf": "vsf"}.get(ext)


def identify(data: bytes, name: str = "", log=None) -> tuple[bytes, dict] | None:
    """``(C64R image, info)`` for a C64 file, or None when ``data`` isn't one."""
    if sniff(data, name) is None:
        return None
    image = analyze(data, name, log=log)
    info = {
        "platform": "c64", "title": image.name or Path(name).stem or "C64 program",
        "game_code": "", "region": "", "size": HEADER + RAM, "compiler_guess": "assembly",
        "sha1": hashlib.sha1(data).hexdigest(), "entry": image.entry, "load": image.load,
        "load_end": image.load_end, "basic_end": image.basic_end, "sys": image.sys,
        "method": image.method, "note": image.note, "basic": image.basic_text,
        "container": image.blocks[0].source if image.blocks else "",
        "original_size": len(data),
        "blocks": [{"name": b.name, "load": b.load, "length": len(b.data), "kind": b.kind} for b in image.blocks],
        "regs": image.regs,
    }
    return pack(image), info


# ---------------------------------------------------------------- containers

def _petscii(raw: bytes) -> str:
    out = []
    for b in raw:
        if b in (0x00, 0xA0):
            break
        out.append(chr(b) if 32 <= b < 127 else "_")
    return "".join(out).rstrip()


def read_prg(data: bytes, name: str = "") -> list[Block]:
    if data.startswith(b"C64File"):
        data = data[26:]
    if len(data) < 3:
        raise RomError("this PRG is too small to carry a load address and a byte")
    load = data[0] | (data[1] << 8)
    return [Block(_petscii(Path(name).stem.encode()) or "PRG", load, data[2:], "prg", "prg")]


def read_t64(data: bytes, name: str = "") -> list[Block]:
    if not (data.startswith(b"C64 tape image file") or data.startswith(b"C64S tape image file")):
        raise RomError("this isn't a T64 (no C64 tape image header)")
    if len(data) < 0x40:
        raise RomError("T64 header is truncated")
    maximum = int.from_bytes(data[0x22:0x24], "little")
    used = int.from_bytes(data[0x24:0x26], "little")
    count = min(used or maximum or 1, maximum or 1, 64)
    blocks = []
    for i in range(count):
        at = 0x40 + i * 32
        if at + 32 > len(data) or data[at] == 0:
            continue
        start = int.from_bytes(data[at + 2:at + 4], "little")
        end = int.from_bytes(data[at + 4:at + 6], "little")
        off = int.from_bytes(data[at + 8:at + 12], "little")
        filename = _petscii(data[at + 16:at + 32]) or f"FILE{i}"
        length = (end - start) & 0xFFFF
        if off > len(data):
            continue
        blob = data[off:off + length] if length else data[off:]
        if length and len(blob) < length:  # a short end pointer: take what is there, up to the next file
            blob = data[off:]
        blocks.append(Block(filename, start, bytes(blob), "prg", "t64"))
    if not blocks:
        raise RomError("this T64 has no files")
    return blocks


def _track_offset(track: int, sector: int) -> int:
    if not 1 <= track <= 40 or sector < 0 or sector >= _SECTORS[track]:
        raise RomError(f"track {track} sector {sector} is off a 1541 disk")
    return sum(_SECTORS[1:track]) * 256 + sector * 256


def _d64_bam_ok(data: bytes) -> bool:
    tracks = _D64_SIZES.get(len(data))
    if tracks is None or len(data) < _track_offset(18, 0) + 4:
        return False
    bam = _track_offset(18, 0)
    return data[bam + 2:bam + 4] == b"A2"


def _sector(data: bytes, track: int, sector: int) -> bytes:
    off = _track_offset(track, sector)
    if off + 256 > len(data):
        raise RomError("this D64 ends before the directory chain does")
    return data[off:off + 256]


def _chain(data: bytes, track: int, sector: int, limit: int = 256) -> bytes:
    """Follow a 1541 sector chain. The last sector's second byte is how many bytes of it count,
    including the two link bytes."""
    out = bytearray()
    seen = set()
    for _ in range(limit):
        if (track, sector) in seen or track == 0:
            break
        seen.add((track, sector))
        sec = _sector(data, track, sector)
        nxt, count = sec[0], sec[1]
        if nxt == 0:
            out.extend(sec[2:max(2, count)])
            break
        out.extend(sec[2:])
        track, sector = nxt, count
    return bytes(out)


def read_d64(data: bytes, name: str = "") -> list[Block]:
    if not _d64_bam_ok(data) and len(data) not in _D64_SIZES:
        raise RomError("this isn't a D64 (size and BAM don't match a 1541 image)")
    if len(data) not in _D64_SIZES:
        raise RomError("this D64's size isn't a 35- or 40-track image")
    blocks: list[Block] = []
    # The first directory sector is 18,1; its link leads to the rest.
    seen = set()
    track, sector = 18, 1
    while track and (track, sector) not in seen and len(seen) < 32:
        seen.add((track, sector))
        sec = _sector(data, track, sector)
        for n in range(8):
            ent = sec[2 + n * 32:2 + (n + 1) * 32]
            if len(ent) < 32:
                continue
            kind_b = ent[0]
            if kind_b == 0 or not kind_b & 0x80:  # empty, or not closed
                continue
            kind = {1: "SEQ", 2: "PRG", 3: "USR", 4: "REL"}.get(kind_b & 7, "DEL")
            if kind == "DEL":
                continue
            ft, fs = ent[1], ent[2]
            filename = _petscii(ent[3:19]) or f"FILE{len(blocks)}"
            if ft == 0:
                continue
            blob = _chain(data, ft, fs)
            if kind == "PRG" and len(blob) >= 2:
                load = blob[0] | (blob[1] << 8)
                blocks.append(Block(filename, load, blob[2:], kind, "d64"))
            else:
                blocks.append(Block(filename, 0, blob, kind, "d64"))
        track, sector = sec[0], sec[1]
    if not blocks:
        raise RomError("this D64 has no closed files")
    return blocks


def gcr_encode(data: bytes) -> bytes:
    """4-to-5 GCR. ``data`` is padded to a multiple of 4 bytes."""
    if len(data) % 4:
        data += bytes(4 - len(data) % 4)
    out = bytearray()
    for i in range(0, len(data), 4):
        bits = 0
        for b in data[i:i + 4]:
            bits = (bits << 10) | (_GCR[b >> 4] << 5) | _GCR[b & 0x0F]
        out.extend(bits.to_bytes(5, "big"))
    return bytes(out)


def gcr_decode(data: bytes) -> bytes:
    """Inverse of ``gcr_encode``. Stops at the first 5-bit group that isn't a GCR symbol."""
    out = bytearray()
    if len(data) % 5:
        data = data[:len(data) - (len(data) % 5)]
    for i in range(0, len(data), 5):
        bits = int.from_bytes(data[i:i + 5], "big")
        nibbles = []
        try:
            for shift in range(35, -1, -5):
                nibbles.append(_UNGCR[(bits >> shift) & 0x1F])
        except KeyError:
            break
        for n in range(0, 8, 2):
            out.append((nibbles[n] << 4) | nibbles[n + 1])
    return bytes(out)


def _g64_sectors(track: bytes) -> dict[int, bytes]:
    """sector number -> 256 data bytes, from one G64 track. Sync runs are $FF; a header is 10 GCR
    bytes (8 decoded, starting with $08) and a data block is 325 GCR bytes (260 decoded, starting
    with $07)."""
    found: dict[int, bytes] = {}
    i, n = 0, len(track)
    while i < n:
        if track[i] != 0xFF:
            i += 1
            continue
        while i < n and track[i] == 0xFF:
            i += 1
        if i >= n:
            break
        window = track[i:i + 10]
        if len(window) < 10:
            break
        header = gcr_decode(window)
        if not header or header[0] != 0x08:
            i += 1
            continue
        sector, _trk = header[2], header[3]
        i += 10
        while i < n and track[i] == 0xFF:
            i += 1
        data = gcr_decode(track[i:i + 325])
        if len(data) >= 257 and data[0] == 0x07:
            found[sector] = data[1:257]
            i += 325
        else:
            i += 1
    return found


def read_g64(data: bytes, name: str = "") -> list[Block]:
    """Best effort. Half-tracks and a disk the decoder can't find a directory on raise RomError;
    turbo layouts and everything past track 40 are not read."""
    if not data.startswith(b"GCR-1541") or len(data) < 12:
        raise RomError("this isn't a G64")
    tracks = data[9]
    offsets = [int.from_bytes(data[12 + 4 * i:16 + 4 * i], "little") for i in range(tracks)]
    # Track 18 is index 34 ((18-1)*2).
    index = 34 if tracks > 34 else None
    if index is None or not offsets[index]:
        raise RomError("this G64 has no track 18 to read a directory from")
    off = offsets[index]
    if off + 2 > len(data):
        raise RomError("this G64's track 18 offset is past the end of the file")
    length = int.from_bytes(data[off:off + 2], "little")
    sectors = _g64_sectors(data[off + 2:off + 2 + length])
    if 1 not in sectors:
        raise RomError("track 18 has no directory sector this reader could decode")
    # Rebuild a tiny D64-shaped view of the sectors we have and reuse the directory walk by
    # stuffing decoded sectors into a sparse image keyed the same way. Easier: walk the directory
    # bytes we already decoded, then fetch file chains from every track we can decode.
    decoded = _g64_all_sectors(data, offsets)
    return _blocks_from_sectors(decoded)


def _g64_all_sectors(data: bytes, offsets: list[int]) -> dict[tuple[int, int], bytes]:
    out: dict[tuple[int, int], bytes] = {}
    for i, off in enumerate(offsets):
        if not off or i % 2:  # half-tracks are odd indexes
            continue
        track = i // 2 + 1
        if off + 2 > len(data):
            continue
        length = int.from_bytes(data[off:off + 2], "little")
        for sector, blob in _g64_sectors(data[off + 2:off + 2 + length]).items():
            out[(track, sector)] = blob
    return out


def _blocks_from_sectors(sectors: dict[tuple[int, int], bytes]) -> list[Block]:
    if (18, 1) not in sectors:
        raise RomError("no directory sector")
    blocks: list[Block] = []
    seen = set()
    track, sector = 18, 1
    while (track, sector) in sectors and (track, sector) not in seen and len(seen) < 32:
        seen.add((track, sector))
        sec = sectors[(track, sector)]
        for n in range(8):
            ent = sec[2 + n * 32:34 + n * 32]
            if len(ent) < 32 or ent[0] == 0 or not ent[0] & 0x80:
                continue
            kind = {1: "SEQ", 2: "PRG", 3: "USR", 4: "REL"}.get(ent[0] & 7, "DEL")
            if kind == "DEL" or ent[1] == 0:
                continue
            blob = _sector_chain(sectors, ent[1], ent[2])
            filename = _petscii(ent[3:19]) or f"FILE{len(blocks)}"
            if kind == "PRG" and len(blob) >= 2:
                load = blob[0] | (blob[1] << 8)
                blocks.append(Block(filename, load, blob[2:], kind, "g64"))
            elif blob:
                blocks.append(Block(filename, 0, blob, kind, "g64"))
        track, sector = sec[0], sec[1]
    if not blocks:
        raise RomError("this G64's directory has no closed files this reader could decode")
    return blocks


def _sector_chain(sectors: dict[tuple[int, int], bytes], track: int, sector: int) -> bytes:
    out = bytearray()
    seen = set()
    while track and (track, sector) not in seen and (track, sector) in sectors and len(seen) < 256:
        seen.add((track, sector))
        sec = sectors[(track, sector)]
        nxt, count = sec[0], sec[1]
        if nxt == 0:
            out.extend(sec[2:max(2, count)])
            break
        out.extend(sec[2:])
        track, sector = nxt, count
    return bytes(out)


def encode_tap(payload: bytes, load: int, name: str = "PROGRAM") -> bytes:
    """A C64-TAPE-RAW (version 0) image of one relocatable program: header and data, each twice,
    the way the KERNAL writes them. Pulses are in units of 8 PAL cycles."""
    header = bytearray(192)
    header[0] = 0x01
    header[1] = load & 0xFF
    header[2] = (load >> 8) & 0xFF
    end = load + len(payload)
    header[3] = end & 0xFF
    header[4] = (end >> 8) & 0xFF
    raw = name.encode("ascii", "replace")[:16]
    header[5:5 + len(raw)] = raw
    pulses = bytearray()
    for block in (bytes(header), bytes(header), payload, payload):
        pulses.extend([_TAP_LONG] * 32)
        for b in block:
            pulses.extend(_tap_byte(b))
    body = bytes(pulses)
    return b"C64-TAPE-RAW" + bytes([0, 0, 0, 0]) + len(body).to_bytes(4, "little") + body


_TAP_SHORT, _TAP_MED, _TAP_LONG = 0x30, 0x42, 0x56


def _tap_byte(value: int) -> list[int]:
    pulses = [_TAP_LONG]
    ones = 0
    for bit in range(8):
        bitv = (value >> bit) & 1
        ones += bitv
        pulses.extend((_TAP_SHORT, _TAP_MED) if bitv == 0 else (_TAP_MED, _TAP_SHORT))
    parity = 0 if ones % 2 else 1  # odd parity over the data bits
    pulses.extend((_TAP_SHORT, _TAP_MED) if parity == 0 else (_TAP_MED, _TAP_SHORT))
    return pulses


def _tap_pulses(data: bytes) -> list[int]:
    version = data[12]
    size = int.from_bytes(data[16:20], "little")
    raw = data[20:20 + size]
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]
        i += 1
        if b == 0 and version >= 1 and i + 2 < len(raw):
            out.append(int.from_bytes(raw[i:i + 3], "little"))
            i += 3
        else:
            out.append(b * 8 if b == 0 else b)
    return out


def _tap_class(value: int) -> str:
    if value >= 0x4C:
        return "long"
    if value >= 0x38:
        return "med"
    return "short"


def decode_tap(data: bytes) -> list[tuple[int, bytes]]:
    """``(load address, payload)`` for every header this decoder can lock onto. A turbo loader is
    not this format; ``tapclean`` can turn one of those into a PRG first (optional, not required)."""
    if not data.startswith(b"C64-TAPE-RAW"):
        raise RomError("this isn't a TAP")
    pulses = _tap_pulses(data)
    data_bytes = bytearray()
    i = 0
    while i < len(pulses):
        if _tap_class(pulses[i]) != "long":
            i += 1
            continue
        # A byte is one long marker and nine short/medium pairs. A failed pair is the
        # leader, not data: start the next try at the following pulse so the real marker
        # is not consumed as a bit.
        start = i
        i += 1
        bits = []
        ok = True
        for _ in range(9):
            if i + 1 >= len(pulses):
                ok = False
                break
            a, b = _tap_class(pulses[i]), _tap_class(pulses[i + 1])
            if a == "short" and b == "med":
                bits.append(0)
            elif a == "med" and b == "short":
                bits.append(1)
            else:
                ok = False
                break
            i += 2
        if not ok or len(bits) < 9:
            i = start + 1
            continue
        value = sum(bit << n for n, bit in enumerate(bits[:8]))
        ones = sum(bits[:8])
        if (ones + bits[8]) % 2 == 1:
            data_bytes.append(value)
    return _tap_programs(bytes(data_bytes))


def _tap_programs(blob: bytes) -> list[tuple[int, bytes]]:
    found = []
    i = 0
    while i + 5 < len(blob):
        if blob[i] in (0x01, 0x03) and blob[i:i + 192].count(blob[i]) < 192:
            load = blob[i + 1] | (blob[i + 2] << 8)
            end = blob[i + 3] | (blob[i + 4] << 8)
            length = (end - load) & 0xFFFF
            if 0 < length < 0x10000:
                # The data block follows the repeated header. Search a window for `length` bytes
                # that aren't another copy of this header.
                window = blob[i + 192:i + 192 + 192 + length + 8]
                # skip a second header if it repeats
                if window[:3] == blob[i:i + 3]:
                    window = window[192:]
                if len(window) >= length:
                    found.append((load, window[:length]))
                    i += 192 + length
                    continue
        i += 1
    return found


def read_tap(data: bytes, name: str = "") -> list[Block]:
    programs = decode_tap(data)
    if not programs:
        raise RomError("this TAP has no KERNAL-format program. A turbo loader needs an optional "
                       "tapclean pass (`tapclean file.tap -doprg`) and then the PRG it writes")
    return [Block(Path(name).stem or f"TAP{n}", load, payload, "prg", "tap")
            for n, (load, payload) in enumerate(programs)]


def read_crt(data: bytes, name: str = "") -> list[Block]:
    if not data.startswith(b"C64 CARTRIDGE") or len(data) < 0x40:
        raise RomError("this isn't a CRT")
    header_len = int.from_bytes(data[0x10:0x14], "big")
    title = _petscii(data[0x20:0x40]) or Path(name).stem or "CART"
    pos = header_len if header_len >= 0x40 else 0x40
    blocks = []
    while pos + 16 <= len(data) and data[pos:pos + 4] == b"CHIP":
        packet = int.from_bytes(data[pos + 4:pos + 8], "big")
        load = int.from_bytes(data[pos + 0x0C:pos + 0x0E], "big")
        size = int.from_bytes(data[pos + 0x0E:pos + 0x10], "big")
        blob = data[pos + 0x10:pos + 0x10 + size]
        blocks.append(Block(title if not blocks else f"{title}-{len(blocks)}", load, bytes(blob), "crt", "crt"))
        pos += packet if packet > 16 else 16 + max(size, 0)
        if packet < 16:
            break
    if not blocks:
        raise RomError("this CRT has no CHIP packets")
    return blocks


_READERS = {"prg": read_prg, "t64": read_t64, "d64": read_d64, "tap": read_tap, "crt": read_crt, "g64": read_g64}


def read_blocks(data: bytes, name: str = "") -> list[Block]:
    kind = sniff(data, name)
    if kind is None:
        raise RomError("this isn't a C64 PRG, T64, D64, TAP, CRT or G64")
    return _READERS[kind](data, name)


# ---------------------------------------------------------------- execution

class _Mem:
    """64 KiB of RAM behind the processor port. Writes to a ROM window hit the RAM under it. Writes
    to I/O (when it's mapped) do not. Reads of BASIC or KERNAL, when mapped, return RTS: this tree
    doesn't ship the copyrighted ROMs, and a JSR into one is turned into a return by the runner."""

    def __init__(self, ram: bytes, load: int, load_end: int):
        self.ram = bytearray(ram)
        if len(self.ram) < RAM:
            self.ram.extend(b"\x00" * (RAM - len(self.ram)))
        self.ddr = 0x2F
        self.port = 0x37
        self.io = bytearray(0x1000)
        self.written = bytearray(RAM)
        self.writes = 0
        self.load, self.load_end = load, load_end

    def __len__(self) -> int:
        return RAM

    def bits(self) -> int:
        return (self.port | (~self.ddr & 0xFF)) & 7

    def windows(self) -> tuple[bool, bool, bool]:
        """(BASIC at $A000, I/O at $D000, KERNAL at $E000) for the port's low three bits."""
        loram, hiram, charen = self.bits() & 1, self.bits() & 2, self.bits() & 4
        basic = bool(loram and hiram)
        kernal = bool(hiram)
        io = bool(charen and (loram or hiram))
        return basic, io, kernal

    def is_rom(self, addr: int) -> bool:
        basic, _io, kernal = self.windows()
        addr &= 0xFFFF
        return (basic and 0xA000 <= addr <= 0xBFFF) or (kernal and addr >= 0xE000)

    def __getitem__(self, addr):
        if isinstance(addr, slice):
            return bytes(self[i] for i in range(*addr.indices(RAM)))
        addr &= 0xFFFF
        if addr == 0:
            return self.ddr
        if addr == 1:
            return self.port
        basic, io, kernal = self.windows()
        if (kernal and addr >= 0xE000) or (basic and 0xA000 <= addr <= 0xBFFF):
            return 0x60
        if io and 0xD000 <= addr <= 0xDFFF:
            return self.io[addr - 0xD000]
        return self.ram[addr]

    def __setitem__(self, addr, value):
        addr &= 0xFFFF
        value &= 0xFF
        if addr == 0:
            self.ddr = value
            return
        if addr == 1:
            self.port = value
            self.written[1] = 1
            return
        _basic, io, _kernal = self.windows()
        if io and 0xD000 <= addr <= 0xDFFF:
            self.io[addr - 0xD000] = value
            return
        self.ram[addr] = value
        if not self.written[addr]:
            self.written[addr] = 1
            self.writes += 1

    def snapshot(self) -> bytes:
        out = bytearray(self.ram[:RAM])
        out[0] = self.ddr
        out[1] = self.port
        return bytes(out)


def _match_len(ram: bytearray, target: int, payload: bytes) -> int:
    """How many bytes at ``target`` are a contiguous slice of the file that was loaded.

    The longest match wins: a decruncher copied to the stack page matches for its whole body, while
    a short accidental match of the same first bytes does not hide that."""
    if target >= RAM or len(payload) < RELOC_MIN:
        return 0
    head = bytes(ram[target:target + RELOC_MIN])
    if len(head) < RELOC_MIN:
        return 0
    best, start = 0, 0
    limit = min(4096, RAM - target)
    while True:
        idx = payload.find(head, start)
        if idx < 0:
            break
        n = RELOC_MIN
        while n < limit and idx + n < len(payload) and ram[target + n] == payload[idx + n]:
            n += 1
        best = max(best, n)
        start = idx + 1
        if best >= limit:
            break
    return best


def _follow(block: Block) -> tuple[bytearray, int, str, str]:
    """Run from the first SYS (or the byte after BASIC, or the load address) until the unpacked
    entry, or give the static entry and a note."""
    basic = c64_basic.decode(block.data, block.load)
    sys_addr = basic.entry
    entry = sys_addr if sys_addr is not None else basic.end if basic.lines else block.load
    ram = bytearray(RAM)
    ram[0] = 0x2F
    ram[1] = 0x37
    end = min(RAM, block.load + len(block.data))
    ram[block.load:end] = block.data[:end - block.load]
    if entry is None or not 0 <= entry < RAM:
        return ram, block.load & 0xFFFF, "static", "no SYS; entry is the load address"
    try:
        from py65.devices.mpu6502 import MPU
    except ImportError:
        return ram, entry, "static", "py65 is not installed; entry is the SYS address, unpackers were not run"
    mem = _Mem(ram, block.load, end)
    mpu = MPU(memory=mem, pc=entry & 0xFFFF)
    mpu.sp = 0xFF
    spans: list[tuple[int, int]] = []
    steps = 0
    while steps < MAX_STEPS:
        pc = mpu.pc & 0xFFFF
        if mem.is_rom(pc):
            return _done(mem, entry, "static", f"execution entered ROM at ${pc:04X}")
        op = mem[pc]
        if op not in mos6502.OPCODES or op == 0x00:
            why = "BRK" if op == 0x00 else f"illegal opcode ${op:02X}"
            return _done(mem, entry, "static", f"{why} at ${pc:04X}; entry stays ${entry:04X}")
        raw = bytes(mem[(pc + i) & 0xFFFF] for i in range(3))
        text, ln = mos6502.decode(raw, pc)
        kind, target = mos6502.flow(text)
        if kind in ("call", "jump") and target is not None:
            if mem.is_rom(target):
                if kind == "call":
                    mpu.pc = (pc + ln) & 0xFFFF
                    steps += 1
                    continue
                return _done(mem, entry, "static", f"jump into ROM at ${target:04X}; entry stays ${entry:04X}")
            if mem.written[target] and not any(lo <= target < hi for lo, hi in spans):
                span = _match_len(mem.ram, target, block.data)
                off = target - block.load
                original = 0 <= off < len(block.data) and mem.ram[target] == block.data[off]
                # A short copy made before anything much is written is the loader relocating itself
                # (the classic stack-page decruncher). Once a lot of memory has been written, the
                # next jump out of that copy is the unpacked program, even when its first bytes also
                # occur as literals in the file.
                if span >= RELOC_MIN and mem.writes < 1024:
                    spans.append((target, min(RAM, target + span)))
                elif not original:
                    return _done(mem, target, "emulated", f"unpacked entry ${target:04X}")
        mpu.step()
        steps += 1
        if steps == EARLY_STEPS and mem.bits() == 7 and mem.writes < 1024:
            # No banking and no big write: nothing is being unpacked. A small relocated routine is
            # the program; otherwise the SYS address still is.
            where = spans[0][0] if spans else entry
            how = "emulated" if spans else "static"
            return _done(mem, where, how, f"no unpacker in the first {EARLY_STEPS} instructions; entry is ${where:04X}")
    return _done(mem, entry, "static", f"stopped after {MAX_STEPS} instructions; entry stays ${entry:04X}")


def _done(mem: _Mem, entry: int, method: str, note: str) -> tuple[bytearray, int, str, str]:
    snap = bytearray(mem.ram[:RAM])
    snap[0] = mem.ddr
    snap[1] = mem.port
    return snap, entry & 0xFFFF, method, note


def _cartridge_entry(block: Block) -> int | None:
    if block.source != "crt" or len(block.data) < 6:
        return None
    if block.data[4:9] == b"CBM80":
        return block.data[0] | (block.data[1] << 8)
    return None


def from_dump(dump: c64_vice.Dump, name: str, method: str) -> Image:
    """An Image from a RAM dump taken after loading (VICE): the BASIC program is found through TXTTAB ($2B)."""
    ram, regs = dump.ram, dump.regs
    txt = ram[0x2B] | ram[0x2C] << 8
    vartab = ram[0x2D] | ram[0x2E] << 8
    basic = None
    if 0x0400 <= txt < 0xA000:
        found = c64_basic.decode(ram[txt:0xA000], txt)
        # BASIC's own end pointer agrees with where the listing ends: a real program, not bytes that look like one
        if found.lines and abs(vartab - found.end) <= 2:
            basic = found
    in_ram = [t for t in (basic.sys if basic else []) if t < 0xA000 or 0xC000 <= t < 0xD000]  # not SYS 64738 (reset)
    entry = in_ram[0] if in_ram else regs.get("pc", 0)
    where = f"PC ${regs.get('pc', 0):04X}"
    return Image(bytes(ram), entry, txt if basic else 0, basic.end if basic else 0, basic.end if basic else 0,
                 in_ram[0] if in_ram else 0, method, f"{method[:4]} {where}", Path(name).stem or "C64",
                 basic.text if basic else "", [], dict(regs))


def analyze(data: bytes, name: str = "", log=None) -> Image:
    """The primary program in ``data``, unpacked when emulation can do it.

    A tape is loaded in VICE (any loader, turbo or not), and the RAM it ends with is the program. On Windows
    decomp.wtf installs its own VICE the first time; DECOMP_C64_VICE=0 turns VICE off. A VICE snapshot (.vsf) is read as it is."""
    kind = sniff(data, name)
    if kind == "vsf":
        return from_dump(c64_vice.read_vsf(data), name, "snapshot")
    if kind == "tap" and os.environ.get("DECOMP_C64_VICE", "1") != "0":
        vice = c64_vice.ensure_vice(log or print)
        if vice is not None:
            return from_dump(c64_vice.dump_tape(data, name, log=log or print, vice=vice),
                             name, "vice")
    blocks = read_blocks(data, name)
    chosen = blocks[0]
    basic = c64_basic.decode(chosen.data, chosen.load)
    for block in blocks:
        program = c64_basic.decode(block.data, block.load)
        if program.entry is not None or _cartridge_entry(block) is not None:
            chosen, basic = block, program
            break
    cart = _cartridge_entry(chosen)
    if cart is not None:
        ram = bytearray(RAM)
        ram[0], ram[1] = 0x2F, 0x37
        end = min(RAM, chosen.load + len(chosen.data))
        ram[chosen.load:end] = chosen.data[:end - chosen.load]
        # A cartridge that starts with CBM80 is entered at its cold-start vector with ROM banked in.
        return Image(bytes(ram), cart, chosen.load, end, chosen.load, cart, "static",
                     f"cartridge cold start ${cart:04X}", chosen.name or Path(name).stem, basic.text, blocks)
    ram, entry, method, note = _follow(chosen)
    return Image(bytes(ram), entry, chosen.load, min(RAM, chosen.load + len(chosen.data)), basic.end,
                 basic.entry or 0, method, note, chosen.name or Path(name).stem or "C64", basic.text, blocks)


_METHODS = {"emulated": 1, "static": 2, "vice": 3, "snapshot": 4}


def pack(image: Image) -> bytes:
    header = bytearray(HEADER)
    header[0:4] = MAGIC
    header[4] = 1
    header[5] = _METHODS.get(image.method, 2)
    if image.regs:  # a VICE dump: the stack pointer and the processor port when the RAM was taken
        header[6], header[7] = image.regs.get("sp", 0) & 0xFF, image.regs.get("p01", 0) & 0xFF
    struct.pack_into("<HHHHH", header, 8, image.entry & 0xFFFF, image.load & 0xFFFF,
                     image.basic_end & 0xFFFF, image.sys & 0xFFFF, image.load_end & 0xFFFF)
    raw = (image.name or "").encode("ascii", "replace")[:30]
    header[18:18 + len(raw)] = raw
    note = image.note.encode("ascii", "replace")[:16]
    header[48:48 + len(note)] = note
    if len(image.ram) != RAM:
        raise RomError("a C64 image needs 64 KiB of RAM")
    return bytes(header) + image.ram


def parse(data: bytes) -> Image:
    if len(data) < HEADER + RAM or data[:4] != MAGIC:
        raise RomError("this isn't a C64 memory image")
    entry, load, basic_end, sys_addr, load_end = struct.unpack_from("<HHHHH", data, 8)
    name = data[18:48].split(b"\x00", 1)[0].decode("ascii", "replace")
    note = data[48:64].split(b"\x00", 1)[0].decode("ascii", "replace")
    method = {v: k for k, v in _METHODS.items()}.get(data[5], "static")
    regs = {"sp": data[6], "p01": data[7]} if method in ("vice", "snapshot") else None
    return Image(data[HEADER:HEADER + RAM], entry, load, load_end, basic_end, sys_addr, method, note, name,
                 regs=regs)


# ---------------------------------------------------------------- functions

def _program_target(ram: bytes, addr: int, entry: int) -> bool:
    if addr == entry:
        return ram[addr] in mos6502.OPCODES and ram[addr] != 0x00
    if not 0 <= addr < RAM or 0xD000 <= addr <= 0xDFFF:
        return False
    if addr in c64_hw.rom_by_addr() and (0xA000 <= addr <= 0xBFFF or addr >= 0xE000):
        return False
    if (0xA000 <= addr <= 0xBFFF or addr >= 0xE000) and ram[addr] == 0:
        return False
    return ram[addr] in mos6502.OPCODES and ram[addr] != 0x00


def _planted_vectors(ram: bytes) -> list[int]:
    """Pointers written with LDA/LDX/LDY #imm then STA/STX/STY to a vector, and the vector bytes
    already in the snapshot."""
    slots: dict[int, int] = {}
    stores = {0x8D: 0xA9, 0x8E: 0xA2, 0x8C: 0xA0}
    i = 0
    while i + 4 < RAM:
        store = ram[i + 2]
        if ram[i] == stores.get(store) and store in stores:
            dest = ram[i + 3] | (ram[i + 4] << 8)
            if dest in _VECTORS:
                slots[dest] = ram[i + 1]
            i += 5
            continue
        i += 1
    found = []
    for lo in (0x0314, 0x0316, 0x0318, 0xFFFA, 0xFFFC, 0xFFFE):
        low = slots.get(lo, ram[lo])
        high = slots.get(lo + 1, ram[lo + 1])
        ptr = low | (high << 8)
        if ptr:
            found.append(ptr)
    return found


def _mem_operand(text: str) -> int | None:
    if text.startswith("db") or "#" in text.split(",")[0]:
        return None
    op, _, rest = text.partition(" ")
    if op in _BRANCHES or op in ("jsr", "jmp"):
        return None
    m = None
    for part in rest.replace("(", " ").replace(")", " ").split(","):
        part = part.strip()
        if part.startswith("$") and len(part) in (3, 5):
            return int(part[1:], 16)
    return m


def functions(ram: bytes, entry: int, *, p01: int | None = None, sp: int | None = None,
              basic: tuple[int, int] | None = None, sys_targets=(), report: dict | None = None
              ) -> tuple[list[gb.GbFunction], list[tuple[int, int, str, str]]]:
    """Every function c64_find finds (certain and probable), plus data islands a branch skips
    (``(rom offset, size, kind, why)``, rom offset counted into a C64R image). ``report`` gets each function's
    confidence (``found``: start -> confidence, score, why) and the coverage."""
    from . import c64_find

    vectors = [] if p01 is not None else [p for p in _planted_vectors(ram) if _program_target(ram, p, entry)]
    start = entry if _program_target(ram, entry, entry) else None
    if basic is not None and start is not None and basic[0] <= start < basic[1]:
        basic = None  # the program was unpacked over its BASIC stub: those bytes are code now
    result = c64_find.find(ram, start, p01=p01, sp=sp, basic=basic, sys_targets=list(sys_targets), vectors=vectors)
    starts = {f.start for f in result.functions}
    out: list[gb.GbFunction] = []
    regions: list[tuple[int, int, str, str]] = []
    found: dict[int, dict] = {}
    for f in result.functions:
        end, islands = c64_find.body(f, starts)
        if end <= f.start:
            continue
        code = bytes(ram[f.start:end])
        refs: list[tuple[int, int, int]] = []
        n_insns = 0
        for addr in sorted(f.insns):
            if not f.start <= addr < end:
                continue
            n_insns += 1
            text, _ln = mos6502.decode(ram[addr:addr + 3], addr)
            kind, target = mos6502.flow(text)
            if kind in ("call", "jump", "cjump") and target is not None:
                refs.append((addr - f.start, 10, target))
            mem = _mem_operand(text)
            if mem is not None and c64_hw.name_of(mem):
                refs.append((addr - f.start, 1, mem))
        asm = c64_hw.name_listing(mos6502.asm_text(code, f.start, width=4, data=islands))
        out.append(gb.GbFunction(name=f"func_{f.start:04x}", vram=f.start, rom=HEADER + f.start, code=code,
                                 n_insns=n_insns, asm=asm, refs=refs, bank=0))
        found[f.start] = {"confidence": f.confidence, "score": f.score, "why": f.reason()}
        for addr, size in islands.items():
            regions.append((HEADER + addr, size, "data", "bytes a branch skips"))
    if report is not None:
        report["found"] = found
        report["coverage"] = result.coverage
    return out, regions


def refs_of(code: bytes, cpu: int, insns: list[int] | None = None) -> list[tuple[int, int, int]]:
    """[offset, kind, address] per call or jump out of a routine (kind 10) and per chip register it uses (kind 1),
    as the import records them. `insns`: the instruction addresses (default: decoded straight through)."""
    refs: list[tuple[int, int, int]] = []
    addrs = insns if insns is not None else [a for a, _t, _n in mos6502.disassemble(code, cpu)]
    for addr in addrs:
        text, _ln = mos6502.decode(code[addr - cpu:addr - cpu + 3], addr)
        kind, target = mos6502.flow(text)
        if kind in ("call", "jump", "cjump") and target is not None:
            refs.append((addr - cpu, 10, target))
        mem = _mem_operand(text)
        if mem is not None and c64_hw.name_of(mem):
            refs.append((addr - cpu, 1, mem))
    return refs


def _jumps_over(insns: dict[int, int], gap: int, resume: int) -> bool:
    for addr, ln in insns.items():
        if addr >= gap:
            continue
        text, _n = mos6502.decode(b"", 0) if False else (None, ln)
        # The branch target was recorded as an instruction at `resume`; reaching it is enough.
        if resume in insns and addr < gap:
            return True
    return resume in insns and any(a < gap for a in insns)


def split(path: Path | bytes, workdir: Path | None = None, log=print, report: dict | None = None,
          hints=None) -> list[gb.GbFunction]:
    """Functions in a C64R image. The image is already unpacked; this does not emulate again."""
    data = path if isinstance(path, (bytes, bytearray)) else Path(path).read_bytes()
    image = parse(bytes(data))
    regs = image.regs or {}
    basic = (image.load, image.basic_end) if 0 < image.load < image.basic_end else None
    found: dict = {}
    fns, regions = functions(image.ram, image.entry, p01=regs.get("p01"), sp=regs.get("sp"), basic=basic,
                             sys_targets=[image.sys] if image.sys else [], report=found)
    if report is not None:
        report["found"] = found.get("found", {})
        report["coverage"] = found.get("coverage", {})
        report["regions"] = regions
        report["entry"] = image.entry
        report["method"] = image.method
        report["note"] = image.note
        report["splitter"] = "c64"
    if log:
        cov = found.get("coverage", {})
        log(f"C64 entry ${image.entry:04X} ({image.method}); {len(fns)} functions: "
            f"{cov.get('certain_functions', 0)} certain, {cov.get('probable_functions', 0)} probable. Code "
            f"{cov.get('certain_bytes', 0)} bytes certain, {cov.get('probable_bytes', 0)} probable, "
            f"{cov.get('unexplained_bytes', 0)} unexplained")
    return fns


# ---------------------------------------------------------------- assembling a documented routine

def _assemble_line(assembler, line: str, pc: int) -> bytes:
    """py65 shortens ``$00xx`` to zero page. A four-digit operand is the absolute form."""
    import re
    widened = re.search(r"\$00([0-9A-Fa-f]{2})(?![0-9A-Fa-f])", line)
    if widened and line.split()[0].lower() not in _BRANCHES:
        forced = line[:widened.start()] + "$01" + widened.group(1) + line[widened.end():]
        encoded = bytearray(assembler.assemble(forced, pc))
        if len(encoded) == 3:
            encoded[2] = 0
            return bytes(encoded)
    return bytes(assembler.assemble(line, pc))


def assemble(source: str, origin: int) -> bytes:
    """6502 mnemonics and ``.byte`` at ``origin``. Names must already be numbers."""
    try:
        from py65.assembler import Assembler
        from py65.devices.mpu6502 import MPU
    except ImportError as e:
        raise RomError("the 6502 assembler (py65) is not installed") from e
    assembler = Assembler(MPU())
    out = bytearray()
    pc = origin & 0xFFFF
    for line_no, raw in enumerate(source.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        if not line or line.endswith(":") or line.startswith(("*", ".org")):
            continue
        if line.lower().startswith(".byte"):
            try:
                encoded = bytes(int(part.strip().replace("$", "0x"), 0) for part in line[5:].split(",") if part.strip())
            except ValueError as e:
                raise RomError(f"line {line_no}: invalid .byte") from e
        else:
            try:
                encoded = _assemble_line(assembler, line, pc)
            except (ValueError, KeyError, IndexError, SyntaxError, OverflowError) as e:
                raise RomError(f"line {line_no}: {e}") from e
        out.extend(encoded)
        pc = (pc + len(encoded)) & 0xFFFF
    if not out:
        raise RomError("no 6502 instructions or .byte values were provided")
    return bytes(out)
