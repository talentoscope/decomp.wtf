"""Bounded binary CUE sheets and indexed track views, without decoding or rewriting assets.

INDEX times are relative to FILE; generated PREGAP/POSTGAP sectors are not stored in that file.
Reference: https://www.gnu.org/software/ccd2cue/manual/html_node/INDEX-_0028CUE-Command_0029.html
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import struct

from . import psx
from .n64 import RomError

MODES = {'AUDIO': (2352, None), 'MODE1/2048': (2048, 0), 'MODE2/2048': (2048, 0),
         'MODE1/2352': (2352, 16), 'MODE2/2352': (2352, 24), 'MODE2/2336': (2336, 8)}
MAX_CUE = 1 << 20


def frames(text: str) -> int:
    match = re.fullmatch(r'(\d{1,3}):(\d{2}):(\d{2})', text)
    if not match:
        raise RomError('CUE index needs MM:SS:FF')
    minutes, seconds, frame = map(int, match.groups())
    if seconds >= 60 or frame >= 75:
        raise RomError('invalid CUE seconds or frame number')
    return (minutes * 60 + seconds) * 75 + frame


def _asset(root: Path, name: str) -> Path:
    relative = name.replace('\\', '/')
    if not relative or relative.startswith('/') or ':' in relative or any(p in ('', '.', '..') for p in relative.split('/')):
        raise RomError('CUE track files must be relative files within the selected folder')
    path = root
    for component in relative.split('/'):
        candidate = path / component
        if not candidate.exists() and path.is_dir():
            choices = [p for p in path.iterdir() if p.name.casefold() == component.casefold()]
            if len(choices) == 1:
                candidate = choices[0]
        path = candidate
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise RomError(f'CUE track file is missing or outside the selected folder: {Path(relative).name}')
    return path.resolve()


@dataclass
class Track:
    number: int
    mode: str
    path: Path
    file: str
    indices: dict[int, int] = field(default_factory=dict)
    pregap: int | None = None
    postgap: int | None = None
    start: int = 0
    end: int = 0

    @property
    def geometry(self):
        return MODES[self.mode]

    def metadata(self) -> dict:
        return {'track': self.number, 'file': self.path.name, 'mode': self.mode, 'indices': self.indices,
                'pregap': self.pregap or 0, 'postgap': self.postgap or 0, 'start': self.start, 'end': self.end}


class TrackStream:
    def __init__(self, track: Track):
        self.file = track.path.open('rb')
        self.start, self.length, self.at = track.start, track.end - track.start, 0
        self.cue_geometry = track.geometry

    def seek(self, offset: int, whence: int = 0):
        offset += 0 if whence == 0 else self.at if whence == 1 else self.length if whence == 2 else -1
        if offset < 0 or whence not in (0, 1, 2):
            raise RomError('invalid seek within CUE track')
        self.at = offset
        return offset

    def tell(self):
        return self.at

    def read(self, size: int = -1) -> bytes:
        count = max(0, self.length - self.at)
        if size >= 0:
            count = min(count, size)
        self.file.seek(self.start + self.at)
        data = self.file.read(count)
        self.at += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


@dataclass
class Sheet:
    path: Path
    tracks: list[Track]

    def select(self, number: int | None = None) -> Track:
        if number is not None and (type(number) is not int or not 1 <= number <= 99):
            raise RomError('cue_track must be a track number from 1 to 99')
        candidates = [t for t in self.tracks if t.mode != 'AUDIO' and (number is None or t.number == number)]
        usable = []
        for track in candidates:
            try:
                with TrackStream(track) as stream:
                    raw, _, platform = psx.from_disc(stream)
                    if platform == 'psx':
                        psx.read_exe(raw)
                    else:
                        from . import ps2

                        ps2.read_elf(raw)
                    usable.append(track)
            except (RomError, IndexError, ValueError, struct.error):
                continue
        if len(usable) > 1:
            raise RomError('CUE contains several bootable data tracks; select cue_track explicitly')
        if not usable:
            raise RomError('no readable PlayStation data track at the declared CUE indices')
        return usable[0]

    def open_data(self, number: int | None = None) -> TrackStream:
        return TrackStream(self.select(number))


def read(path: Path) -> Sheet:
    path = Path(path)
    if path.stat().st_size > MAX_CUE:
        raise RomError('CUE sheet exceeds 1 MiB')
    data = path.read_bytes()
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('cp1252')
    tracks, current, source, source_name, files = [], None, None, '', set()
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(';'):
            continue
        command = line.split(None, 1)[0].upper()
        if command in ('REM', 'TITLE', 'PERFORMER', 'SONGWRITER', 'CATALOG', 'ISRC', 'FLAGS'):
            continue
        file = re.fullmatch(r'FILE\s+(?:"([^"]+)"|(\S+))\s+(\w+)\s*(?:;.*)?', line, re.I)
        if file:
            if file[3].upper() != 'BINARY':
                raise RomError('only binary CUE track files are supported')
            source_name = file[1] or file[2]
            source = _asset(path.parent, source_name)
            if source in files or len(files) >= 99:
                raise RomError('CUE FILE entries must be unique and at most 99')
            files.add(source)
            current = None
            continue
        line = line.split(';', 1)[0].strip()
        track = re.fullmatch(r'TRACK\s+(\d{1,2})\s+(\S+)', line, re.I)
        if track:
            number, mode = int(track[1]), track[2].upper()
            if source is None or mode not in MODES or not 1 <= number <= 99 or (tracks and number <= tracks[-1].number):
                raise RomError('invalid CUE track order, file or mode')
            current = Track(number, mode, source, source_name)
            tracks.append(current)
            continue
        index = re.fullmatch(r'INDEX\s+(\d{1,2})\s+(\S+)', line, re.I)
        gap = re.fullmatch(r'(PREGAP|POSTGAP)\s+(\S+)', line, re.I)
        if current and index:
            number, at = int(index[1]), frames(index[2])
            if number in current.indices or (current.indices and (number <= max(current.indices) or at < max(current.indices.values()))) or current.postgap is not None:
                raise RomError('CUE indices must be unique and ordered')
            current.indices[number] = at
        elif current and gap:
            kind = gap[1].lower()
            if getattr(current, kind) is not None or (kind == 'pregap' and current.indices) or (kind == 'postgap' and 1 not in current.indices):
                raise RomError('invalid CUE gap order')
            setattr(current, kind, frames(gap[2]))
        else:
            raise RomError(f'unsupported or malformed CUE command at line {line_number}')
    if not tracks:
        raise RomError('CUE sheet contains no tracks')
    for source in files:
        members = [t for t in tracks if t.path == source]
        if not members:
            raise RomError('CUE FILE has no tracks')
        if len({t.geometry[0] for t in members}) != 1:
            raise RomError('mixed sector widths in one CUE file need separate track files')
        width = members[0].geometry[0]
        size = source.stat().st_size
        if size % width:
            raise RomError('CUE track file has a truncated sector')
        for i, track in enumerate(members):
            if 1 not in track.indices or (0 in track.indices and track.indices[0] > track.indices[1]):
                raise RomError('each CUE track needs an ordered INDEX 01')
            track.start = track.indices[1] * width
            following = members[i + 1] if i + 1 < len(members) else None
            if following is not None and 1 not in following.indices:
                raise RomError('each CUE track needs INDEX 01')
            track.end = min(following.indices.values()) * width if following else size
            if not 0 <= track.start < track.end <= size or any(v * width >= track.end for k, v in track.indices.items() if k >= 1):
                raise RomError('CUE track indices exceed their file extent or overlap')
    return Sheet(path, tracks)


def digest(path: Path) -> str:
    """Composite identity pins the sheet and every referenced binary, without publishing filesystem paths."""
    path = Path(path)
    sheet = read(path)
    result = hashlib.sha256(b'decomp-cue-v1\0' + path.read_bytes())
    for source in dict.fromkeys(t.path for t in sheet.tracks):
        value = hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 20), b''):
                value.update(block)
        result.update(value.digest())
    return result.hexdigest()


@contextmanager
def open_image(path: Path):
    """A binary file or a CUE's unique bootable data-track view, owned by this context."""
    path = Path(path)
    if path.suffix.lower() == '.cue':
        with read(path).open_data() as stream:
            yield stream
    else:
        with path.open('rb') as stream:
            yield stream
