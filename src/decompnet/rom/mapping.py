"""Versioned, data-only descriptions of executable regions in a user's image.

Version 1 deliberately supports PS1 binary files and ISO9660 disc files only. A map
can describe several independently loaded regions, including overlapping banks.
It supplies addresses; byte reconstruction is a separate check from that evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from . import psx, split


class MappingError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    pass


def _pairs(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=deep)
        if not isinstance(key, str) or key in result:
            raise MappingError(f"duplicate or non-string mapping key: {key!r}")
        result[key] = loader.construct_object(value, deep=deep)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _pairs)
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _obj(value, keys, required, where):
    if not isinstance(value, dict) or set(value) - set(keys) or set(required) - set(value):
        raise MappingError(f"{where}: expected fields {', '.join(required)}; allowed: {', '.join(keys)}")
    return value


def _number(value, where):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+|[0-9]+", value):
        result = int(value, 16 if value.startswith("0x") else 10)
    else:
        raise MappingError(f"{where}: expected an integer or hexadecimal string")
    if not 0 <= result <= 0xFFFFFFFF:
        raise MappingError(f"{where}: outside the unsigned 32-bit range")
    return result


def _text(value, where, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MappingError(f"{where}: expected nonempty text of at most {limit} characters")
    return value


def _hash(value, where):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise MappingError(f"{where}: expected a lowercase SHA-256 digest")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    if Path(path).suffix.lower() == ".cue":
        from .cue import digest as cue_digest

        return cue_digest(Path(path))
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class Region:
    id: str
    file: str
    source_sha256: str
    offset: int
    size: int
    vram: int
    kind: str
    visible: tuple[str, ...]
    confidence: str
    evidence: str
    functions: tuple[dict, ...]

    def to_dict(self):
        return dict(id=self.id, file=self.file, source_sha256=self.source_sha256,
                    offset=self.offset, size=self.size, vram=self.vram, kind=self.kind,
                    visible=list(self.visible), confidence=self.confidence,
                    evidence=self.evidence, functions=list(self.functions))


@dataclass(frozen=True)
class Profile:
    title: str
    format: str
    sha256: str
    gp: int
    regions: tuple[Region, ...]

    @staticmethod
    def parse(text: str):
        if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
            raise MappingError("profile must be text of at most 1 MiB")
        try:
            obj = yaml.load(text, Loader=_Loader)
        except (yaml.YAMLError, RecursionError) as e:
            raise MappingError(f"invalid profile: {e}") from None
        obj = _obj(obj, ("version", "platform", "title", "input", "gp", "regions"),
                   ("version", "platform", "title", "input", "regions"), "profile")
        if type(obj["version"]) is not int or obj["version"] != 1 or obj["platform"] != "psx":
            raise MappingError("mapping version 1 supports platform 'psx' only")
        inp = _obj(obj["input"], ("format", "sha256"), ("format", "sha256"), "input")
        if inp["format"] not in ("binary", "psx-disc"):
            raise MappingError("input.format must be binary or psx-disc")
        if not isinstance(obj["regions"], list) or not 1 <= len(obj["regions"]) <= 256:
            raise MappingError("expected between 1 and 256 regions")
        regions = []
        for raw in obj["regions"]:
            raw = _obj(raw, ("id", "file", "source_sha256", "offset", "size", "vram", "kind", "visible",
                             "confidence", "evidence", "functions"),
                       ("id", "source_sha256", "offset", "size", "vram", "kind", "confidence", "evidence"), "region")
            name = raw["id"]
            if not isinstance(name, str) or not _ID.fullmatch(name) or any(r.id == name for r in regions):
                raise MappingError("region ids must be unique identifiers (letter, then letters/digits/underscore)")
            filename = raw.get("file", "")
            if inp["format"] == "psx-disc":
                if not isinstance(filename, str) or not filename or "\\" in filename or ":" in filename or ";" in filename or \
                        PurePosixPath(filename).is_absolute() or any(p in ("", ".", "..") for p in filename.split("/")):
                    raise MappingError(f"{name}: file must be a relative ISO9660 path")
                filename = filename.upper()
            elif filename:
                raise MappingError(f"{name}: binary profiles read the supplied file; omit region.file")
            offset, size, vram = (_number(raw[k], f"{name}.{k}") for k in ("offset", "size", "vram"))
            if not size or offset + size > 0xFFFFFFFF or not 0x80000000 <= vram < vram + size <= 0x80200000:
                raise MappingError(f"{name}: invalid size or PS1 cached-RAM extent")
            if raw["kind"] not in ("code", "data"):
                raise MappingError(f"{name}: kind must be code or data")
            if raw["kind"] == "code" and any(n % 4 for n in (offset, size, vram)):
                raise MappingError(f"{name}: MIPS code offsets, sizes and addresses must be word aligned")
            if raw["confidence"] not in ("verified", "derived"):
                raise MappingError(f"{name}: confidence must be verified or derived")
            visible = raw.get("visible", [])
            if not isinstance(visible, list) or any(not isinstance(v, str) for v in visible) or len(set(visible)) != len(visible):
                raise MappingError(f"{name}: visible must list unique region ids")
            functions = raw.get("functions", [])
            if not isinstance(functions, list) or len(functions) > 10000 or functions and raw["kind"] != "code":
                raise MappingError(f"{name}: functions must be a list for a code region")
            checked = []
            for fn in functions:
                fn = _obj(fn, ("offset", "size", "name", "kind"), ("offset", "size"), f"{name}.function")
                at, length = _number(fn["offset"], "function.offset"), _number(fn["size"], "function.size")
                label = fn.get("name", f"func_{vram + at:08X}")
                if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z_]\w{0,127}", label, re.ASCII):
                    raise MappingError("function.name must be a C identifier")
                if not length or at % 4 or length % 4 or at + length > size:
                    raise MappingError(f"{name}: function outside region or not word aligned")
                if fn.get("kind", "code") not in ("code", "asm"):
                    raise MappingError("function.kind must be code or asm")
                checked.append(dict(offset=at, size=length, name=label, kind=fn.get("kind", "code")))
            checked.sort(key=lambda f: f["offset"])
            if any(a["offset"] + a["size"] > b["offset"] for a, b in zip(checked, checked[1:])) or \
                    len({f["name"] for f in checked}) != len(checked):
                raise MappingError(f"{name}: overlapping functions or duplicate names")
            regions.append(Region(name, filename, _hash(raw["source_sha256"], f"{name}.source_sha256"),
                                  offset, size, vram, raw["kind"], tuple(visible), raw["confidence"],
                                  _text(raw["evidence"], f"{name}.evidence"), tuple(checked)))
        by_id = {r.id: r for r in regions}
        if sum(r.size for r in regions) > 64 * 1024 * 1024:
            raise MappingError("version 1 profiles may map at most 64 MiB of extracted regions")
        for r in regions:
            if any(v not in by_id or v == r.id for v in r.visible):
                raise MappingError(f"{r.id}: visible references an unknown region or itself")
            view = [r, *(by_id[v] for v in r.visible)]
            for i, a in enumerate(view):
                for b in view[i + 1:]:
                    if a.vram < b.vram + b.size and b.vram < a.vram + a.size:
                        raise MappingError(f"{r.id}: visible regions {a.id} and {b.id} overlap in RAM")
        gp = _number(obj.get("gp", 0), "gp")
        if gp and not 0x80000000 <= gp < 0x80200000:
            raise MappingError("gp must be zero or a PS1 cached-RAM address")
        return Profile(_text(obj["title"], "title", 200), inp["format"], _hash(inp["sha256"], "input.sha256"),
                       gp, tuple(regions))

    def to_dict(self):
        return dict(version=1, platform="psx", title=self.title, input=dict(format=self.format, sha256=self.sha256),
                    gp=self.gp, regions=[r.to_dict() for r in self.regions])

    def render(self):
        return json.dumps(self.to_dict(), indent=2) + "\n"


@dataclass
class Prepared:
    profile: Profile
    directory: Path
    data: dict[str, bytes]
    functions: dict[str, list[split.RomFunction]]
    symbols: dict[str, list[split.DataSymbol]]


def prepare(profile: Profile, image: Path, directory: Path, log=lambda *_: None) -> Prepared:
    """Extract and analyse into a NEW scratch directory. Never writes the source image or a project."""
    image, directory = Path(image), Path(directory)
    if file_digest(image) != profile.sha256:
        raise MappingError("input SHA-256 differs from the profile; check the game's version")
    if directory.exists():
        raise MappingError("analysis directory must not exist")
    directory.mkdir(parents=True)
    data, functions, symbols = {}, {}, {}
    from .cue import open_image

    with open_image(image) as stream:
        source_size = getattr(stream, "length", image.stat().st_size)
        class BoundedDisc(psx._Disc):
            def read(self, lba, length):
                if lba < 0 or length < 0 or length > 64 * 1024 * 1024 or \
                        (lba + (length + 2047) // 2048) * self.size > source_size:
                    raise MappingError("disc file extent is outside the image or exceeds the v1 64 MiB source limit")
                return super().read(lba, length)

            def sector(self, lba):
                result = super().sector(lba)
                if len(result) != 2048:
                    raise MappingError("disc sector is truncated")
                return result

        disc = BoundedDisc(stream) if profile.format == "psx-disc" else None
        sources = {}
        for region in profile.regions:
            # Read a disc file once. Binary inputs are sliced on disk instead of loaded whole.
            if disc is not None:
                if region.file not in sources:
                    sources.clear()  # bound memory even if a profile refers to many different large files
                    try:
                        blob = disc.find(region.file)
                    except (IndexError, struct.error, psx.RomError) as e:
                        raise MappingError(f"malformed ISO9660 directory: {e}") from None
                    if blob is None:
                        raise MappingError(f"disc file not found: {region.file}")
                    sources[region.file] = blob
                blob = sources[region.file]
                if digest(blob) != region.source_sha256 or region.offset + region.size > len(blob):
                    raise MappingError(f"{region.id}: source hash differs or region exceeds the disc file")
                data[region.id] = blob[region.offset:region.offset + region.size]
            else:
                if profile.sha256 != region.source_sha256 or region.offset + region.size > image.stat().st_size:
                    raise MappingError(f"{region.id}: source hash differs or region exceeds the input")
                stream.seek(region.offset)
                data[region.id] = stream.read(region.size)
            log(f"analysing {region.id}: {region.size} bytes at 0x{region.vram:08X} ({region.confidence})")
            work = directory / region.id
            work.mkdir()
            (work / "region.bin").write_bytes(data[region.id])
            functions[region.id], symbols[region.id] = [], []
            if region.kind == "data":
                continue
            opts = dict(platform="psx", compiler="GCC", basename=region.id, base_path=".", target_path="region.bin",
                        symbol_addrs_path="symbols.txt", disassemble_all=True, auto_decompile_empty_functions=False,
                        find_file_boundaries=False, gp_value=profile.gp)
            syms = []
            for fn in region.functions:
                syms.append(f"{fn['name']} = 0x{region.vram + fn['offset']:08X}; // type:func size:0x{fn['size']:X}")
            (work / "symbols.txt").write_text("\n".join(syms) + "\n", encoding="utf-8")
            config = dict(name=region.id, options=opts, segments=[dict(name=region.id, type="code", start=0,
                          vram=region.vram, subsegments=[[0, "c", region.id]]), [region.size]])
            (work / "splat.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            split._splat(["split", "splat.yaml"], work)
            for path in sorted((work / "asm/nonmatchings").glob("**/*.s")):
                fn = split.parse_function(path, region.id, False)
                if fn is None:
                    continue
                split._complete_delay_slot(fn, data[region.id], False)
                if fn.rom < 0 or fn.rom + fn.size > region.size:
                    raise MappingError(f"{region.id}: disassembler emitted a function outside its region")
                if fn.vram != region.vram + fn.rom:
                    raise MappingError(f"{region.id}: disassembly address disagrees with the profile")
                original = data[region.id][fn.rom:fn.rom + fn.size]
                reconstructed = b"".join(line.word.to_bytes(4, "little") for line in fn.lines)
                if reconstructed != original:
                    raise MappingError(f"{region.id}: disassembly bytes differ from the dump")
                fn.name = f"{region.id}__{fn.name}"
                functions[region.id].append(fn)
            found = sorted(functions[region.id], key=lambda f: f.rom)
            if any(a.rom + a.size > b.rom for a, b in zip(found, found[1:])):
                raise MappingError(f"{region.id}: disassembler emitted overlapping functions")
            for hint in region.functions:
                fn = next((f for f in found if f.rom == hint["offset"]), None)
                if fn is None or fn.size != hint["size"] or fn.name != f"{region.id}__{hint['name']}":
                    raise MappingError(f"{region.id}: disassembly disagrees with a function boundary hint")
            for path in (work / "asm/data").glob("**/*.s"):
                symbols[region.id].extend(s for s in split.parse_data(path)
                                          if region.vram <= s.vram < s.vram + s.size <= region.vram + region.size)
            if not functions[region.id]:
                raise MappingError(f"{region.id}: no functions found; review code boundaries and function hints")
    # Detect an input modified while analysis was in progress, before any state can be applied.
    if file_digest(image) != profile.sha256:
        raise MappingError("input changed during analysis")
    return Prepared(profile, directory, data, functions, symbols)
