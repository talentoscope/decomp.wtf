"""Banked ROM project import, shared function metadata and bank layout.

The engine delegates import here; analysis remains in each console lane. Keeping project
bookkeeping separate lets mapper and identity changes avoid the main orchestration file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

BANKED_TOOLS = {"sm83": ("gbdk-2020", "rgbds", "mgbdis"), "z80": ("gbdk-2020", "wla-dx"), "mos6502": ("gbdk-2020", "cc65")}


def remember_input(net, slug: str, info: dict) -> None:
    """Attach another observed dump identity without replacing the project's frozen ROM metadata."""
    from ..rom.sms_identity import merge_inputs

    incoming = info.get("identity")
    if incoming is None:
        return

    def change(cfg):
        cfg["rom"]["identity"] = merge_inputs(cfg["rom"].get("identity"), incoming)

    net._update_cfg(slug, change)


def with_edges(meta: str | None, edges: dict | None) -> str | None:
    """A function's meta with where its calls into the Game Boy's switchable bank go (offset -> ROM offset)."""
    m = json.loads(meta or "{}")
    if edges:
        m["edges"] = {str(k): v for k, v in sorted(edges.items())}
    else:
        m.pop("edges", None)
    return json.dumps(m) if m else None


def segments(platform: str, info: dict, report: dict) -> list[dict]:
    """One segment per bank, at the CPU address its code runs at: as the Master System lane found the game maps it
    (`report["bank_base"]`), else where the bank is shown at power-on (see rom/gb.py, rom/sms.py and rom/nes.py)."""
    from ..rom import gb, nes, sms

    if platform == "nes":
        return nes.segments(info)
    sega = platform in ("sms", "gg")
    lane = sms if sega else gb
    bases = report.get("bank_base") or {}
    return [{"name": f"bank{b:03d}", "rom_start": b * lane.BANK_SIZE, "rom_end": min((b + 1) * lane.BANK_SIZE, info["size"]),
             "vram": int(bases.get(b, bases.get(str(b), sms.segment_vram(b, info["size"]) if sega else (0 if b == 0 else 0x4000))))}
            for b in range(max(1, -(-info["size"] // lane.BANK_SIZE)))]


def import_project(net, picked, slug: str, workdir: Path, log, context_header: str) -> str:
    """Game Boy / Game Boy Color (SM83) and Master System / Game Gear (Z80): functions found by the fork's own
    disassemblers (mgbdis and rgbs for the Game Boy when installed), banks included.

    The matching lane is live when the pinned GBDK-2020 toolchain is installed: then functions are claimable
    (agents compile through rom/gb_build) and the profile names it. Without it the project stays readable-only
    (the functions are shown as assembly), which is the honest state rather than handing out work nobody can
    judge."""
    from ..rom import gb, gb_build, nes, sms

    info = picked.info
    sega = picked.platform in ("sms", "gg")
    famicom = picked.platform == "nes"
    lane, arch = (sms, "z80") if sega else (nes, "mos6502") if famicom else (gb, "sm83")
    label = "Master System / Game Gear" if sega else "NES" if famicom else "Game Boy"
    net._ensure_tools(BANKED_TOOLS[arch], log)
    report: dict = {}
    fns = lane.split(workdir / picked.filename, workdir, log=log, report=report)
    matchable = gb_build.available(arch)
    # The toolchain is named even when it is not installed: the project's overview reads it (and says it is missing).
    compiler = {"toolchain": gb_build.TOOLCHAIN, "flags": ["--fsigned-char"], "timeout": 120, "wrapper": "cc",
                "provenance": "explicit" if matchable else "unavailable",
                "note": ("SDCC/GBDK-2020 through decompnet.rom.gb_build (absolute symbol mode)" if matchable else
                         f"the {label} C toolchain (GBDK-2020) is not installed: install it from Setup, or import the game again")}
    layout = segments(picked.platform, info, report)
    from ..rom import fingerprint

    built = fingerprint.identify(picked.platform, (workdir / picked.filename).read_bytes())
    if built:
        log(f"built with {built['tool']}{' ' + built['version'] if built['version'] else ''}"
            + (f" ({built['language']})" if built["language"] else "") + (f": {built['note']}" if built["note"] else ""))
    else:
        log("no compiler's start-up code or runtime in it: most likely hand-written assembly, which the map and the "
            "assembly track are for")
    cfg = {
        "kind": "rom", "platform": info["platform"], "big_endian": False, "gp": 0, "arch": arch,
        "splitter": report.get("splitter", "built-in"),  # what found the functions: "built-in" is approximate for the Game Boy
        "rom": {**info, "file": picked.filename, "program": picked.filename},
        "segments": layout,
        "compiler": compiler,
        "built_with": built,  # (None: no known toolchain's code in it, most likely assembly)
        "legal": {"basis": "personal-copy", "license": "personal use only",
                  "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                  "confirmed_at": time.time()},
        "context": {"file": None},
    }
    if sega:
        from ..rom.sms_profile import initial
        cfg["sms_profile"] = initial(cfg["rom"])
        log("cartridge mapping is unresolved: the Sega-based listing is provisional until declared in Settings")
    mode = info["region"] if sega or famicom else info["cgb"]["mode"]
    now = time.time()
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         (slug, info["title"],
                          f"{info['cartridge']} · {mode} · {len(fns)} functions · "
                          + ("provisional Sega-based listing" if sega else "matched with SDCC/GBDK-2020" if matchable
                             else "reading lane only (no C toolchain yet)")
                          + (" · approximate split (mgbdis and rgbds not installed)"
                             if arch == "sm83" and report.get("splitter") == "built-in" else ""),
                          arch, 0, str(workdir), json.dumps(cfg))).lastrowid
        names: set[str] = set()
        for f in fns:
            name = f.name
            if name in names:  # two banks can share a CPU address; the bank in the name prevents it
                name = f"{name}_{f.rom:06x}"
            names.add(name)
            db.execute(
                "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status,"
                "meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                # vram is the CPU address the ROM's instructions use; rom_offset the flat file offset;
                # the unit is the bank, so briefs and context edits read per bank.
                (pid, name, f.size, f.n_insns, f.code, json.dumps([list(r) for r in f.refs or []]),
                 f.asm, None, f"bank{f.bank:03d}", f.vram, f.rom, "open" if matchable else "asm",
                 with_edges(None, getattr(f, "edges", None))))
            db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                       (pid, name, f.vram, f.size, "func", None))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, context_header, None, "basic types", now, ""))
        net._store_regions(db, pid, report.get("regions") or [])
        net._event(db, pid, None, "import", f"{len(fns)} functions from {info['title']}")
    if sega:
        log(f"ready: {len(fns)} functions in a provisional listing. Declare the cartridge mapping in Settings "
            "before accepting source.")
    elif matchable:
        log(f"ready: {len(fns)} functions to decompile with SDCC/GBDK-2020 ({gb_build.TOOLCHAIN})")
    else:
        log(f"ready: {len(fns)} functions disassembled. The C toolchain (GBDK-2020) is not installed, "
            f"so they are shown as assembly until it is")
    return slug
