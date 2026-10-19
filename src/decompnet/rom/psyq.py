"""Conservative PSY-Q library identification from user-provided wildcard signatures.

No signature database is bundled or downloaded. The accepted JSON shape is the public per-library format:
[{"name": "object.OBJ", "sig": "00 11 ?? ...", "labels": [{"name": "symbol", "offset": 0}]}].
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time

MAX_BYTES = 50 << 20
MAX_RECORDS = 50_000
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SDK_HINTS = {"330": (("gcc-2.6.0-psx", "gcc-2.6.3-psx"), "2.21"), "350": (("gcc-2.6.0-psx", "gcc-2.6.3-psx"), "2.34"),
             "3610": (("gcc-2.7.2-psx",), "2.34"), "3611": (("gcc-2.7.2-psx",), "2.34"),
             "400": (("gcc-2.7.2-psx",), "2.56"), "410": (("gcc-2.7.2-cdk",), "2.67"),
             "430": (("gcc-2.8.0-psx",), "2.77"), "440": (("gcc-2.8.1-psx",), "2.79"),
             "450": (("gcc-2.91.66-psx",), "2.81"), "460": (("gcc-2.95.2-psx",), "2.86")}


@dataclass(frozen=True)
class Signature:
    object: str
    version: str
    pattern: tuple[int | None, ...]
    labels: tuple[tuple[str, int], ...]
    anchor: bytes
    anchor_at: int
    key: str


def parse(text: str, version: str = "unknown") -> tuple[list[Signature], int]:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("signature file exceeds 50 MiB")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", version):
        raise ValueError("SDK version must be a short version label")
    records = json.loads(text)
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("expected a JSON list with at most 50,000 signatures")
    out, skipped = [], 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("sig"), str):
            raise ValueError("each signature needs a sig string")
        if len(record["sig"]) > 3 * (1 << 20) + 4096:
            raise ValueError("individual signature exceeds 1 MiB of pattern bytes")
        tokens = record["sig"].split()
        if len(tokens) > 1 << 20 or any(not re.fullmatch(r"[0-9A-Fa-f]{2}|\?\?", t) for t in tokens):
            raise ValueError("signature bytes must be hexadecimal pairs or ?? wildcards")
        pattern = tuple(None if t == "??" else int(t, 16) for t in tokens)
        if len(pattern) < 24 or sum(t is not None for t in pattern) < 16:
            skipped += 1
            continue
        longest, longest_at, run, run_at = b"", 0, bytearray(), 0
        for i, value in enumerate((*pattern, None)):
            if value is not None:
                if not run:
                    run_at = i
                run.append(value)
            else:
                if len(run) > len(longest):
                    longest, longest_at = bytes(run), run_at
                run.clear()
        if len(longest) < 4:
            skipped += 1
            continue
        labels = []
        if not isinstance(record.get("labels"), list):
            raise ValueError("each signature needs a labels list")
        for label in record["labels"]:
            if not isinstance(label, dict):
                raise ValueError("invalid signature label")
            name, offset = label.get("name"), label.get("offset")
            if isinstance(name, str) and IDENT.fullmatch(name) and isinstance(offset, int) and 0 <= offset < len(pattern) and offset % 4 == 0:
                labels.append((name, offset))
        if not labels:
            skipped += 1
            continue
        out.append(Signature(str(record.get("name", "object"))[:120], version, pattern, tuple(labels), longest, longest_at,
                             "".join(tokens).upper()))
    return out, skipped


def unique_offset(data: bytes, signature: Signature, deadline: float) -> tuple[int | None, bool]:
    """A unique full-pattern hit, or no result; never truncate an ambiguous search into a unique hit."""
    at, found = 0, None
    while True:
        if time.monotonic() >= deadline:
            return None, True
        hit = data.find(signature.anchor, at)
        if hit < 0:
            return found, False
        at = hit + 1
        start = hit - signature.anchor_at
        if start < 0 or start % 4 or start + len(signature.pattern) > len(data):
            continue
        if all(value is None or data[start + i] == value for i, value in enumerate(signature.pattern)):
            if found is not None:
                return None, False
            found = start


def scan(regions: list[tuple[str, bytes, int, int]], signatures: list[Signature], seconds: float = 60) -> dict:
    """Regions are (unit, bytes, load address, file offset); uniqueness is checked independently per unit."""
    deadline = time.monotonic() + seconds
    labels, version_hits = {}, {}
    truncated = False
    for unit, data, vram, flat in regions:
        offsets = {}
        for signature in signatures:
            if time.monotonic() >= deadline:
                truncated = True
                break
            if signature.key not in offsets:
                offsets[signature.key] = unique_offset(data, signature, deadline)
            offset, expired = offsets[signature.key]
            if expired:
                truncated = True
                break
            if offset is None:
                continue
            for name, relative in signature.labels:
                key = (unit, flat + offset + relative, vram + offset + relative)
                entry = labels.setdefault(key, {})
                evidence = entry.setdefault(name, {"versions": set(), "objects": set(), "end": 0})
                evidence["versions"].add(signature.version)
                evidence["objects"].add(signature.object)
                evidence["end"] = max(evidence["end"], flat + offset + len(signature.pattern))
                version_hits.setdefault(signature.version, set()).add((*key, name))
        if truncated:
            break
    hits, conflicts = [], []
    for (unit, flat, address), names in sorted(labels.items()):
        if len(names) != 1:
            conflicts.append({"unit": unit, "rom_offset": flat, "address": address, "names": sorted(names)})
            continue
        name, evidence = next(iter(names.items()))
        hits.append({"unit": unit, "rom_offset": flat, "address": address, "name": name,
                     "matched_end": evidence["end"], "versions": sorted(evidence["versions"]), "objects": sorted(evidence["objects"])})
    versions = sorted(({"version": v, "symbols": len(names)} for v, names in version_hits.items()),
                      key=lambda r: (-r["symbols"], r["version"]))
    best = [r["version"] for r in versions if r["symbols"] == versions[0]["symbols"]] if versions else []
    return {"hits": hits, "conflicts": conflicts, "versions": versions, "best_sdk_versions": best,
            "toolchain_hints": [{"sdk": v, "toolchains": list(SDK_HINTS[v][0]), "aspsx": SDK_HINTS[v][1]} for v in best if v in SDK_HINTS],
            "truncated": truncated,
            "note": "SDK evidence describes library objects. Game code may use a different compiler. Names do not establish C prototypes."}
