"""Bounded PS1 compiler experiments, ranked using the normal linked byte comparison.

Only score summaries and configuration recipes leave this module; generated drafts stay local in memory.
Results are hypotheses because a draft can be wrong even when the original toolchain is available.
"""

from __future__ import annotations

import json
import time

from .. import handwritten, scoring, seeds, toolchains
from ..project import compiler_settings
from ..psx_compile import ASPSX_VERSIONS


def rank(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (-r["exact"], -r["percent"], -r["compiled"], r["toolchain"], str(r["flags"]), r["aspsx"]))


def confidence(rows: list[dict], sample_size: int) -> str:
    if not rows or sample_size < 3 or not rows[0]["compiled"]:
        return "inconclusive"
    if len(rows) > 1 and (rows[0]["exact"], rows[0]["percent"]) == (rows[1]["exact"], rows[1]["percent"]):
        return "tied"
    if len(rows) > 1 and rows[0]["exact"] == sample_size and rows[0]["exact"] > rows[1]["exact"]:
        return "strong"
    return "suggestive" if len(rows) > 1 and rows[0]["percent"] - rows[1]["percent"] >= 2 else "inconclusive"


def variations(profile: dict, assembler_available: bool) -> list[dict]:
    """Change one factor at a time after the initial compiler comparison."""
    flags = profile["flags"]
    out = []
    for prefix, choices in (("-O", ["-O0", "-O1", "-O2"]), ("-G", ["-G0", "-G4", "-G8", "-G4096"]),
                            ("-f", ["-fsigned-char", "-funsigned-char"])):
        keep, at, skip = [], None, False
        for flag in flags:
            if skip:
                skip = False
                continue
            matching = flag in choices if prefix == "-f" else flag.startswith(prefix)
            if matching:
                at = len(keep) if at is None else at
                skip = flag == "-G"
            else:
                keep.append(flag)
        at = len(keep) if at is None else at
        out += [profile | {"flags": keep[:at] + [choice] + keep[at:]} for choice in choices]
    if assembler_available:
        out += [profile | {"aspsx": version} for version in ("", *ASPSX_VERSIONS)]
    return out


def run(net, proj, unit: str | None = None, expanded: bool = False, log=print) -> dict:
    cfg = net.cfg(proj)
    functions = net.store.all("SELECT * FROM functions WHERE project_id=? AND status='open' AND n_insns BETWEEN 3 AND 32 "
                              "ORDER BY n_insns, name", proj["id"])
    if unit is None:
        resident = [f["unit"] for f in functions if not (f["unit"] or "").startswith("ovl_")]
        unit = max(set(resident), key=lambda u: (resident.count(u), u or "")) if resident else None
    functions = [f for f in functions if f["unit"] == unit]
    limit, budget, seconds = (8, 512, 240) if expanded else (4, 96, 60)
    deadline = time.monotonic() + seconds
    samples = []
    if seeds.available():
        for fn in functions:
            if len(samples) >= limit or time.monotonic() >= deadline:
                break
            if json.loads(fn["meta"] or "{}").get("kind") == "library":
                continue
            if handwritten.mips_reason(fn["target"], False, "psx"):
                continue
            header = net._context(proj["id"], fn["unit"])["header"]
            draft = seeds.generate_from_asm(net._asm_text(fn, proj), fn["name"], "gcc-2.7.2-psx",
                                            header, timeout=3, valid=True)
            if draft and "M2C_ERROR(" not in draft:
                samples.append((fn, header, seeds.M2C_MACROS + "\n" + draft))
    settings = compiler_settings(cfg, unit)
    available = [t for t in toolchains.registry().values() if t.platform == "PS1" and toolchains.installed(t.id)]
    report = {"unit": unit, "samples": [f["name"] for f, _, _ in samples], "results": [], "compiles": 0,
              "confidence": "inconclusive", "truncated": False,
              "baseline": {k: settings.get(k) for k in ("toolchain", "flags", "aspsx")},
              "note": "Draft quality limits identification. Library code must be excluded. Settings are not changed."}
    if not samples:
        report["note"] = "No eligible compilable drafts; install m2c or select a unit with small open C functions."
        return report
    seen = set()

    def evaluate(profile):
        key = json.dumps(profile, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        if report["compiles"] + len(samples) > budget or time.monotonic() >= deadline:
            report["truncated"] = True
            return
        candidate = cfg | {"compiler": settings | profile}
        candidate["compiler"].pop("cmd", None)
        candidate["compiler"]["timeout"] = min(float(settings.get("timeout", 20)), 5)
        experiment = dict(proj) | {"config": json.dumps(candidate)}
        exact, compiled, percent = 0, 0, 0.0
        for fn, header, draft in samples:
            if time.monotonic() >= deadline:
                report["truncated"] = True
                return  # partially measured configurations cannot compete with complete rows
            result = net._compile_function(experiment, fn, header, draft)
            report["compiles"] += 1
            if not result.ok:
                continue
            cand, verdict, _ = net._evaluate(fn, experiment, result.obj)
            if cand is None:
                continue
            compiled += 1
            diff = scoring.diff(net._target_obj(fn, experiment), cand)
            matches = diff.bytes_match if verdict is None else verdict
            exact += bool(matches)
            percent += 100.0 if matches else min(99.99, diff.percent)
        report["results"].append(profile | {"compiled": compiled, "exact": exact, "percent": round(percent / len(samples), 4)})
        log(f"{profile['toolchain']} {' '.join(profile['flags'])} ASPSX {profile['aspsx'] or 'native'}: "
            f"{exact}/{len(samples)} exact")

    for tc in available:
        evaluate({"toolchain": tc.id, "flags": settings.get("flags", tc.flags), "aspsx": settings.get("aspsx", "") or ""})
    leaders = rank(report["results"])[:2 if expanded else 1]
    assembler_available = bool(toolchains.installed("maspsx") and toolchains.installed("binutils-mips"))
    for leader in leaders:
        profile = {k: leader[k] for k in ("toolchain", "flags", "aspsx")}
        for variant in variations(profile, assembler_available):
            evaluate(variant)
    report["results"] = rank(report["results"])
    report["confidence"] = confidence(report["results"], len(samples))
    return report
