"""Work out what the user picked: a console ROM (N64, GBA, Game Boy/Color), a PlayStation
executable, or a PlayStation disc image."""

from __future__ import annotations

from dataclasses import dataclass, field

import struct
from pathlib import Path

from . import chd, gb, gba, n64, nes, overlays, ps2, psx, sms
from .n64 import RomError

PLATFORMS = {"n64": "Nintendo 64", "psx": "PlayStation", "ps2": "PlayStation 2",
             "gba": "Game Boy Advance", "gb": "Game Boy", "gbc": "Game Boy Color",
             "sms": "Sega Master System", "gg": "Sega Game Gear", "nes": "Nintendo Entertainment System"}
SMALL = 64 * 1024 * 1024  # ROMs and programs are read whole; disc images are read from disk


@dataclass
class Picked:
    platform: str  # n64 | psx | ps2 | gba | gb | gbc | sms | gg | nes
    data: bytes  # what we split: a z64 ROM, a PS-X EXE or a PS2 ELF
    filename: str  # name to store it under in the project folder
    info: dict
    big_endian: bool
    overlays: list = field(default_factory=list)  # (name, bytes, load address): code in other files on the disc
    debug_files: list = field(default_factory=list)  # sanitized basename and local debug bytes


def _program(data: bytes, name: str = "") -> Picked | None:
    if gba.is_gba(data):
        return Picked("gba", data, "rom.gba", gba.read_info(data), False)
    if nes.is_nes(data):
        return Picked("nes", data, "rom.nes", nes.read_info(data, name), False)
    if gb.is_gb(data):
        info = gb.read_info(data)
        return Picked(info["platform"], data, f"rom.{'gbc' if info['platform'] == 'gbc' else 'gb'}", info, False)
    if sms.is_sms(data, name):
        info = sms.read_info(data, name)
        data = sms.strip_copier_header(data)
        return Picked(info["platform"], data, f"rom.{info['platform']}", info, False)
    if data[:4] in n64.MAGIC:
        rom, info = n64.read_info(data)
        return Picked("n64", rom, "rom.z64", info.to_dict() | {"platform": "n64", "gp": 0}, True)
    if data[:8] == b"PS-X EXE":
        exe, info = psx.read_exe(data, name)
        return Picked("psx", exe, "game.exe", info, False)
    if ps2.is_mips_elf(data):
        elf, info = ps2.read_elf(data, name)
        return Picked("ps2", elf, "game.elf", info, False)
    return None


def identify(src: bytes | Path, name: str = "", cue_track: int | None = None) -> Picked:
    """What the user picked: a ROM or program (bytes or a file), or a disc image (read from the file in place)."""
    if cue_track is not None and (not isinstance(src, (str, Path)) or Path(src).suffix.lower() != ".cue"):
        raise RomError("cue_track applies to a local CUE sheet")
    if isinstance(src, (str, Path)):
        path = Path(src)
        name = name or path.name
        if path.suffix.lower() == ".cue":
            from . import cue

            sheet = cue.read(path)
            selected = sheet.select(cue_track)
            with cue.TrackStream(selected) as stream:
                picked = _from_disc(stream)
            picked.info["disc"].update({"format": "cue", "track": selected.number,
                                        "tracks": [t.metadata() for t in sheet.tracks]})
            return picked
        with open(path, "rb") as f:
            head = f.read(8)
        if chd.is_chd(head):  # a compressed disc image: read in place, a hunk at a time
            with chd.Chd(path) as disc:
                if psx.is_disc(disc):
                    picked = _from_disc(disc)
                    picked.info["disc"]["format"] = "chd"
                    return picked
            raise RomError("this CHD doesn't hold a PlayStation disc this app can read")
        if path.stat().st_size <= SMALL:
            return identify(path.read_bytes(), name)
        with open(path, "rb") as f:
            if psx.is_disc(f):
                return _from_disc(f)
        raise RomError("this file is too big for a ROM and isn't a disc image this app can read")
    picked = _program(src, name)
    if picked is not None:
        return picked
    if name.lower().endswith(".cue"):
        raise RomError("import a CUE sheet by its local path, with its binary tracks in the same folder")
    if psx.is_disc(src):
        return _from_disc(src)
    if src[:4] == b"\x7fELF":
        raise RomError("this ELF program isn't for a console decomp.wtf can split from scratch yet")
    raise RomError("this isn't a ROM, disc or game program this app can read: pick an N64 ROM (.z64, .n64, .v64), "
                   "a Game Boy or Game Boy Color ROM (.gb, .gbc), a Game Boy Advance ROM (.gba), an NES ROM (.nes), "
                   "a Master System or Game Gear ROM (.sms, .gg), a PlayStation 1 "
                   "or 2 disc image (.bin, .img, .iso, .cue, .chd) or its program file (e.g. SLUS_200.00)")


def _from_disc(src) -> Picked:
    raw, boot, platform = psx.from_disc(src)
    picked = _program(raw, boot)
    if picked is None:
        raise RomError(f"the disc's program {boot} isn't a PlayStation executable this app can read")
    disc = psx._Disc(src)
    picked.info["disc"] = {"format": "iso" if disc.size == 2048 else "raw", "sector_size": disc.size, "skip": disc.skip}
    if picked.platform == "psx":
        from .psyqsym import discover

        try:
            picked.debug_files = discover(psx._Disc(src))
        except (RomError, struct.error, IndexError):
            pass
        main = (picked.info["load"], picked.info["load"] + picked.info["size"] - 0x800)
    else:
        segs = overlays._elf_segments(raw) or []
        main = (min((a for a, _ in segs), default=0), max((a + len(d) for a, d in segs), default=0))
    try:
        discovery = {}
        picked.overlays = overlays.find(psx._Disc(src), boot, picked.platform, main, discovery)
        picked.info["overlay_discovery"] = discovery
    except (RomError, struct.error, IndexError):  # an odd disc: the program alone still works
        picked.overlays = []
    return picked
