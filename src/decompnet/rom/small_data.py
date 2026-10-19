"""PS1 small-data evidence; inferred symbol extents are hints rather than proven C object sizes."""

from __future__ import annotations

from bisect import bisect_right
import json

_MEMORY = set(range(0x20, 0x2F)) | {0x31, 0x32, 0x39, 0x3A}
THRESHOLDS = (0, 4, 8, 4096)
_GP_OPS = _MEMORY | {9}


def threshold(flags: list[str], default: int = 8) -> int | None:
    value = default
    for i, flag in enumerate(flags):
        raw = flags[i + 1] if flag == "-G" and i + 1 < len(flags) else flag[2:] if flag.startswith("-G") else None
        if raw is not None:
            try:
                value = int(raw)
                if value < 0:
                    return None
            except ValueError:
                return None
    return value


def with_threshold(flags: list[str], value: int) -> list[str]:
    out, skip = [], False
    for flag in flags:
        if skip:
            skip = False
        elif flag == "-G":
            skip = True
        elif not flag.startswith("-G"):
            out.append(flag)
    return out + [f"-G{value}"]


def analyze(functions: list[dict], symbols: list[dict], gp: int) -> dict:
    table = sorted((s for s in symbols if s.get("kind") != "func" and s.get("size", 0) > 0),
                   key=lambda s: (s["address"], -s["size"]))
    starts = [s["address"] for s in table]
    units = {}
    for fn in functions:
        meta = fn.get("meta") or {}
        meta = json.loads(meta) if isinstance(meta, str) else meta
        if meta.get("kind") == "library":
            continue
        unit = fn.get("unit") or ""
        row = units.setdefault(unit, {"functions": 0, "gp_accesses": 0, "resolved_accesses": 0, "objects": {}})
        row["functions"] += 1
        code = fn["target"] or b""
        for offset in range(0, len(code) - 3, 4):
            word = int.from_bytes(code[offset:offset + 4], "little")
            if (word >> 21) & 31 != 28 or word >> 26 not in _GP_OPS:
                continue
            row["gp_accesses"] += 1
            if not gp:
                continue
            address = (gp + ((word & 0xFFFF) ^ 0x8000) - 0x8000) & 0xFFFFFFFF
            at = bisect_right(starts, address) - 1
            if at < 0:
                continue
            symbol = table[at]
            if not symbol["address"] <= address < symbol["address"] + symbol["size"]:
                continue
            row["resolved_accesses"] += 1
            item = row["objects"].setdefault(symbol["name"], {"name": symbol["name"], "address": symbol["address"],
                                                           "estimated_size": symbol["size"], "accesses": 0})
            item["accesses"] += 1
    for row in units.values():
        objects = sorted(row["objects"].values(), key=lambda s: (-s["estimated_size"], s["name"]))
        largest = max((o["estimated_size"] for o in objects), default=0)
        row["objects"] = objects[:128]
        row["largest_estimated_object"] = largest
        row["layout_candidates"] = [n for n in THRESHOLDS if n > 0 and n >= largest] if row["gp_accesses"] else list(THRESHOLDS)
        row["default_threshold"] = 8 if row["gp_accesses"] else 0
        row["confidence"] = "provisional" if row["gp_accesses"] else "inconclusive"
        row["note"] = ("GP-relative accesses require a compatible small-data recipe. Symbol sizes are inferred extents; "
                       "layout candidates need compile verification." if row["gp_accesses"] else
                       "No GP accesses were observed in these functions. G0 is a matching starting point; absence does not identify the original flag.")
    resident = [(name, row) for name, row in units.items() if not name.startswith("ovl_")]
    default_unit = max(resident, key=lambda item: (item[1]["functions"], item[0]))[0] if resident else next(iter(units), "")
    return {"gp": gp, "gp_known": bool(gp), "default_unit": default_unit, "units": units,
            "note": "Full-address accesses do not prove an upper threshold: extern declarations and section choices can suppress GP addressing."}
