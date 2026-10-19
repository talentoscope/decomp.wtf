"""Bounded readers for PsyQ MND symbols, text maps and CPE load images.

The readers retain typed evidence and sanitized source basenames. They do not execute input, emit game source
or assume a debug file describes the current executable; address and byte validation belong to the importer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bisect
import re
import struct

MAX_FILE = 32 << 20
MAX_RECORDS = 200_000
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BASE = {1: "void", 2: "signed char", 3: "short", 4: "int", 5: "long", 6: "float", 7: "double",
         12: "unsigned char", 13: "unsigned short", 14: "unsigned int", 15: "unsigned long"}


class DebugFormatError(ValueError):
    pass


def basename(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name.replace("\\", "/").rsplit("/", 1)[-1])[:128]


class Reader:
    def __init__(self, data: bytes, at: int = 0):
        if len(data) > MAX_FILE:
            raise DebugFormatError("debug file exceeds 32 MiB")
        self.data, self.at = data, at

    def take(self, length: int) -> bytes:
        if length < 0 or self.at + length > len(self.data):
            raise DebugFormatError(f"truncated debug record at offset 0x{self.at:X}")
        value = self.data[self.at:self.at + length]
        self.at += length
        return value

    def number(self, length: int = 4, signed: bool = False) -> int:
        return int.from_bytes(self.take(length), "little", signed=signed)

    def string(self) -> str:
        value = self.take(self.number(1)).rstrip(b"\0")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("shift_jis", "replace")


@dataclass
class DebugInfo:
    format: str
    symbols: list[dict] = field(default_factory=list)
    functions: dict[int, dict] = field(default_factory=dict)
    types: list[dict] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    loads: list[tuple[int, bytes]] = field(default_factory=list)
    entry: int | None = None
    run_address: int | None = None


def declaration(record: dict, name: str | None = None, parameters: str = "") -> str | None:
    """Render the common scalar/pointer/function/array COFF forms; retain unsupported types as evidence."""
    name = name or record["name"]
    if not IDENT.fullmatch(name):
        return None
    kind = record["type"]
    base = _BASE.get(kind & 15)
    if base is None:
        tag = record.get("tag", "")
        if kind & 15 not in (8, 9, 10) or not IDENT.fullmatch(tag):
            return None
        base = {8: "struct", 9: "union", 10: "enum"}[kind & 15] + " " + tag
    derived = []
    kind >>= 4
    while kind:
        derived.append(kind & 3)
        kind >>= 2
    if any(d == 0 for d in derived):
        return None
    if not derived or all(d == 1 for d in derived):
        return base + " " + "*" * len(derived) + name
    if derived[0] == 2 and all(d == 1 for d in derived[1:]):
        return base + " " + "*" * (len(derived) - 1) + name + "(" + parameters + ")"
    if derived[:2] == [1, 2] and all(d == 1 for d in derived[2:]):
        return base + " " + "*" * (len(derived) - 2) + "(*" + name + ")()"
    dims = record.get("dimensions", [])
    arrays = sum(d == 3 for d in derived)
    if arrays and len(dims) == arrays and all(0 < n <= 1 << 20 for n in dims):
        suffix = "".join(f"[{n}]" for n in dims)
        if derived == [3] * arrays + [1] * (len(derived) - arrays):
            return base + " " + "*" * (len(derived) - arrays) + name + suffix
        if derived == [1] * (len(derived) - arrays) + [3] * arrays:
            return base + " (" + "*" * (len(derived) - arrays) + name + ")" + suffix
    return None


def _typed_record(reader: Reader, value: int, complex_type: bool) -> dict:
    record = {"value": value, "class": reader.number(2), "type": reader.number(2), "size": reader.number()}
    if complex_type:
        count = reader.number(2)
        if count > 16:
            raise DebugFormatError("too many array dimensions")
        record["dimensions"] = [reader.number() for _ in range(count)]
        tag = reader.string()
        record["tag"] = tag if IDENT.fullmatch(tag) else ""
    record["name"] = reader.string()
    record["declaration"] = declaration(record)
    if record["class"] in (1, 9):
        record["home"] = {"kind": "frame_offset" if record["class"] == 1 else "argument_slot",
                          "offset": value if value < 0x80000000 else value - 0x100000000}
    elif record["class"] in (4, 17):
        record["home"] = {"kind": "register" if value < 32 else "unknown_register", "index": value}
    return record


def read_sym(data: bytes) -> DebugInfo:
    if data[:4] != b"MND\x01" or len(data) < 8:
        raise DebugFormatError("expected a PsyQ MND version 1 symbol file")
    reader, info = Reader(data, 8), DebugInfo("sym")
    current, source, line, count = None, "", 0, 0
    declarations = {}
    while reader.at < len(data):
        if data[reader.at:reader.at + 5] == b"\0" * min(5, len(data) - reader.at) and not any(data[reader.at:]):
            break
        count += 1
        if count > MAX_RECORDS:
            raise DebugFormatError("too many symbol records")
        at, value = reader.at, reader.number()
        tag = reader.number(1)
        if tag in (1, 2, 5, 6):
            name = reader.string()
            if tag != 1 and IDENT.fullmatch(name):
                info.symbols.append({"name": name, "address": value, "kind": "func" if tag == 2 else "unknown", "file": source})
        elif tag in (0x80, 0x82, 0x84, 0x86):
            amount = 0 if tag == 0x86 else (1 if tag == 0x80 else reader.number(1 if tag == 0x82 else 2))
            if tag == 0x86:
                line = reader.number()
            info.lines.append({"address": value, "file": source, "line": line})
            line += amount
        elif tag == 0x88:
            line, source = reader.number(), basename(reader.string())
        elif tag == 0x8A:
            source, line = "", 0
        elif tag == 0x8C:
            frame_reg, frame_size, return_reg, mask, mask_offset, first_line = struct.unpack("<HIHIiI", reader.take(20))
            file, name = basename(reader.string()), reader.string()
            if not IDENT.fullmatch(name):
                name = f"func_{value:08X}"
            current = {"name": name, "address": value, "file": file, "line": first_line,
                       "frame_register": frame_reg, "frame_size": frame_size, "return_register": return_reg,
                       "saved_register_mask": mask, "saved_register_offset": mask_offset, "locals": [], "parameters": []}
            info.functions[value] = current
            info.symbols.append({"name": name, "address": value, "kind": "func", "file": file})
        elif tag in (0x8E, 0x90, 0x92):
            number = reader.number()
            if current is not None and tag == 0x8E:
                current.update({"end": value, "end_line": number})
                current = None
        elif tag in (0x94, 0x96):
            record = _typed_record(reader, value, tag == 0x96)
            if record["class"] == 103:
                source = basename(record["name"])
            if record["class"] in (2, 3) and IDENT.fullmatch(record["name"]):
                function_type = (record["type"] >> 4) & 3 == 2
                info.symbols.append({"name": record["name"], "address": value, "kind": "func" if function_type else "data",
                                     "file": source, "size": record["size"]})
                if function_type:
                    declarations[value] = record
            if current is not None and record["class"] in (1, 4, 9, 17):
                destination = current["parameters"] if record["class"] in (9, 17) else current["locals"]
                if len(destination) < 128:
                    destination.append(record)
            if record["class"] in (8, 10, 11, 12, 13, 15, 16, 18, 102):
                info.types.append(record)
        else:
            raise DebugFormatError(f"unsupported MND chunk 0x{tag:02X} at offset 0x{at:X}")
    for address, fn in info.functions.items():
        record = declarations.get(address)
        if record:
            params = [p["declaration"] for p in fn["parameters"]]
            fn["prototype"] = declaration(record, fn["name"], ", ".join(params) if all(params) else "")
    return info


def read_map(data: bytes) -> DebugInfo:
    Reader(data)
    text = data.decode("utf-8", "replace")
    info, seen = DebugInfo("map"), set()
    for line in text.splitlines():
        section = re.fullmatch(r"\s*([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s+[0-9A-Fa-f]{8}\s+\S+\s+(\.\w+)\s*", line)
        if section:
            start, end, size, name = section.groups()
            info.sections.append({"address": int(start, 16), "end": int(end, 16) + 1, "size": int(size, 16), "name": name})
            continue
        address_first = re.fullmatch(r"\s*(?:0x)?([0-9A-Fa-f]{8})\s+([A-Za-z_][A-Za-z0-9_]{0,127})\s*", line)
        name_first = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]{0,127})\s*=\s*(?:0x)?([0-9A-Fa-f]{8})\s*;?\s*", line)
        if address_first:
            value, name = address_first.groups()
        elif name_first:
            name, value = name_first.groups()
        else:
            continue
        address = int(value, 16)
        if (name, address) not in seen:
            seen.add((name, address))
            info.symbols.append({"name": name, "address": address, "kind": "unknown", "file": ""})
            if len(seen) > MAX_RECORDS:
                raise DebugFormatError("too many map symbols")
    if not info.symbols and not info.sections:
        raise DebugFormatError("no PsyQ map sections or address/name records found")
    return info


def read_cpe(data: bytes) -> DebugInfo:
    if data[:4] != b"CPE\x01":
        raise DebugFormatError("expected a PsyQ CPE version 1 load image")
    reader, info = Reader(data, 4), DebugInfo("cpe")
    for _ in range(MAX_RECORDS):
        tag = reader.number(1)
        if tag == 0:
            if any(data[reader.at:]):
                raise DebugFormatError("unexpected data after CPE end marker")
            return info
        if tag == 1:
            address, size = reader.number(), reader.number()
            if size > MAX_FILE or address + size > 0x100000000:
                raise DebugFormatError("invalid CPE load range")
            info.loads.append((address, reader.take(size)))
        elif tag == 2:
            info.run_address = reader.number()  # undocumented run address; the PC register record sets entry
        elif tag in (3, 4, 5, 6):
            register = reader.number(2)
            value = reader.number({3: 4, 4: 2, 5: 1, 6: 3}[tag])
            if register == 0x90:
                info.entry = value
        elif tag in (7, 8):
            reader.take(4 if tag == 7 else 1)
        else:
            raise DebugFormatError(f"unsupported CPE chunk 0x{tag:02X}")
    raise DebugFormatError("too many CPE records")


def read(data: bytes, name: str = "") -> DebugInfo:
    if data[:4] == b"MND\x01":
        return read_sym(data)
    if data[:4] == b"CPE\x01":
        return read_cpe(data)
    if name.lower().endswith(".map"):
        return read_map(data)
    raise DebugFormatError("expected an MND .SYM, PsyQ .MAP or CPE .CPE file")


def discover(disc) -> list[tuple[str, bytes]]:
    from .overlays import disc_files

    out, total = [], 0
    for path, lba, size in disc_files(disc):
        if not path.lower().endswith((".sym", ".map", ".cpe")) or not 0 < size <= MAX_FILE:
            continue
        total += size
        if total > 64 << 20 or len(out) >= 64:
            break
        out.append((basename(path), disc.read(lba, size)))
    return out


def inspect(files: list[tuple[str, bytes]], regions: list[tuple[str, bytes, int]], functions: list[dict],
            unit: str | None = None) -> dict:
    """Plan an import. A name needs a unique mapped unit; CPE evidence must agree byte for byte."""
    if len(files) > 64 or sum(len(data) for _, data in files) > 64 << 20:
        raise DebugFormatError("provide at most 64 debug files, up to 64 MiB total")
    regions = [r for r in regions if unit is None or r[0] == unit]
    if not regions:
        raise DebugFormatError("no mapped regions in the selected unit")
    parsed = [(basename(name), read(data, name)) for name, data in files]
    checked = 0
    for _, info in parsed:
        for address, data in info.loads:
            candidates = [raw[address - base:address - base + len(data)] for _, raw, base in regions
                          if base <= address and address + len(data) <= base + len(raw)]
            if len(candidates) != 1 or candidates[0] != data:
                raise DebugFormatError(f"CPE load at 0x{address:08X} is ambiguous, unmapped or differs from this image")
            checked += len(data)
    indexed = {(f["unit"], f["vram"]): f for f in functions}
    hits, skipped, conflicts = {}, 0, set()
    types = []
    for filename, info in parsed:
        types.extend(info.types[:128])
        lines = sorted(info.lines, key=lambda row: row["address"])
        addresses = [row["address"] for row in lines]
        for symbol in info.symbols:
            address = symbol["address"]
            units = {u for u, raw, base in regions if base <= address < base + len(raw)}
            if len(units) != 1:
                skipped += 1
                continue
            owner = next(iter(units))
            fn = indexed.get((owner, address))
            if symbol["kind"] == "func" and fn is None:
                skipped += 1
                continue
            key = (owner, address, symbol["name"])
            details = dict(info.functions.get(address, {}))
            if fn:
                first = bisect.bisect_left(addresses, address)
                last = bisect.bisect_left(addresses, address + fn["size"])
                details["lines"] = lines[first:min(last, first + 256)]
            record = {**symbol, "unit": owner, "debug_file": filename, "function_id": fn["id"] if fn else None,
                      "details": details}
            hits[key] = record
    owners = {}
    for key, hit in hits.items():
        old = owners.setdefault(hit["name"], (hit["unit"], hit["address"]))
        if old != (hit["unit"], hit["address"]):
            conflicts.add(hit["name"])
    return {"hits": [h for h in hits.values() if h["name"] not in conflicts], "skipped": skipped,
            "conflicts": len(conflicts), "checked_bytes": checked, "types": types[:128],
            "files": [{"name": name, "format": info.format, "symbols": len(info.symbols), "lines": len(info.lines)}
                      for name, info in parsed],
            "confidence": "matching load bytes" if checked else "address agreement only; verify the build identity",
            "note": "Names are aliases. Types and register/stack homes are proposals; shared C declarations are unchanged."}
