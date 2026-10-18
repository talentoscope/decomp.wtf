"""Export a Game Boy project as an RGBDS project that rebuilds the game byte for byte.

Most Game Boy games are assembly, so this is the base for mods, translations and any port: the disassembly with
the project's names and what its agents found out, rebuilt with RGBDS and checked against the user's copy. The tree
holds the project's work (baserom.sym: names, code and data, for mgbdis; comments.txt; the routines documented in
assembly, in documented/; the matched functions' C in c/, for reference). The disassembly itself is made from the
user's copy by build.py and stays out of git; build.py puts each documented routine's text in place of its bytes.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..limits import capped
from ..network import NetworkError
from . import gb_asm, gb_hw, sm83
from .gb import BANK_SIZE
from .gb_mgbdis import labels
from .n64 import RomError

LABEL = re.compile(r"^[A-Za-z_][\w@#$]*$")  # an RGBDS label (no dot: that's a local one)
NOTES_PER_LABEL, NOTE_CHARS = 2, 400
DOCUMENTED = "documented"  # the routines documented in assembly, one file each, and the names they use
WORD = re.compile(r"(?<![.\w])[A-Za-z_][\w]*")


def export_gb(net, slug: str, out: Path, log=print) -> dict:
    proj = net._project(slug)
    cfg = net.cfg(proj)
    rom = Path(proj["root"]) / cfg["rom"]["file"]
    image = rom.read_bytes()
    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    base = f"baserom{rom.suffix if rom.suffix in ('.gb', '.gbc') else '.gbc'}"

    rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY rom_offset", proj["id"])
    regions = net.store.all("SELECT * FROM data_regions WHERE project_id=? ORDER BY rom_offset", proj["id"])
    documented, left_out = _documented(net, proj, rows, log)
    blocks = _blocks(rows, regions, len(image), [(r["rom_offset"], r["size"]) for r in documented])
    named = [f"{(r['rom_offset'] or 0) // BANK_SIZE:02x}:{r['vram'] or 0:04x} {r['name']}" for r in rows if LABEL.match(r["name"])]
    log("disassembling your copy with mgbdis, to see where its labels are")
    try:  # every function starts a .code block, so mgbdis decodes an instruction there for its name
        at_labels = labels(rom, sym="\n".join(blocks + named) + "\n")
    except RomError as e:
        raise NetworkError(f"mgbdis can't disassemble this game: {e}") from None
    sym, notes, matched, unplaced = list(blocks), [], 0, []
    placing = {r["id"] for r in documented}
    for r in rows:
        bank, addr = (r["rom_offset"] or 0) // BANK_SIZE, r["vram"] or 0
        why = json.loads(r["meta"] or "{}").get("asm_code") or ""
        if not LABEL.match(r["name"]):
            continue
        if (bank, addr) not in at_labels:  # mgbdis is mid-instruction there: a name would have no label to go on
            unplaced.append(r["name"])
            if r["id"] in placing:  # (build.py finds a documented routine's bytes by its label)
                placing.discard(r["id"])
                left_out.append((r["name"], "mgbdis puts no label at its start"))
            continue
        sym.append(f"{bank:02x}:{addr:04x} {r['name']}")
        if why.startswith("padding") and r["status"] == "asm":
            sym.append(f"{bank:02x}:{addr:04x} .data:{r['size']:x}")  # shown as bytes, not as nops
        said = [f"Set aside: {why}"] if why and r["status"] == "asm" else []
        if r["id"] in placing:
            said.append(f"Documented in assembly: {DOCUMENTED}/{r['name']}.asm (build.py puts it in place of its bytes)")
        if r["status"] == "matched" and r["matched_source"]:
            (out / "c").mkdir(exist_ok=True)
            (out / "c" / f"{r['name']}.c").write_text(
                f"/* {r['name']} (bank {bank}, ${addr:04x}): SDCC (GBDK-2020, flags {' '.join(cfg['compiler']['flags'])})"
                f" compiles this to exactly the original code, with context.h. */\n#include \"context.h\"\n\n"
                f"{r['matched_source'].strip()}\n", encoding="utf-8")
            said.append(f"Matched in C: c/{r['name']}.c")
            matched += 1
        for n in net.store.all("SELECT text FROM notes WHERE function_id=? ORDER BY id DESC LIMIT ?", r["id"], NOTES_PER_LABEL):
            text = " ".join(n["text"].split())
            said.append(text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS - 3] + "...")
        notes += [f"{r['name']}: {line}" for text in said for line in textwrap.wrap(text, 110)]
    (out / "baserom.sym").write_text(
        "; for mgbdis: where each function starts (.code:length in hex, so it decodes from there), what isn't code\n"
        "; (.data:length: tables, padding, bytes the code never reaches), and names (bank:address Name)\n"
        + "\n".join(sym) + "\n", encoding="utf-8")
    (out / "comments.txt").write_text("\n".join(notes) + ("\n" if notes else ""), encoding="utf-8")
    if matched:
        (out / "c" / "context.h").write_text(net._context(proj["id"])["header"], encoding="utf-8")
    placed = [r for r in documented if r["id"] in placing]
    entries = _write_documented(net, proj, out, placed, {line.split()[1] for line in sym if not line.split()[1].startswith(".")})

    (out / "build.json").write_text(json.dumps({"title": proj["name"], "platform": cfg.get("platform"), "image": base,
                                                "sha1": hashlib.sha1(image).hexdigest(), "documented": entries},
                                               indent=1), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("export_gb_build.py"), out / "build.py")
    shutil.copyfile(rom, out / base)
    (out / ".gitignore").write_text(f"build/\n{base}\n", encoding="utf-8")
    _docs(out, proj["name"], base, matched, len(rows), len(entries))

    log("building the exported project to check it rebuilds the game byte for byte")
    proc = subprocess.run(capped([sys.executable, "build.py"]), cwd=out, capture_output=True, text=True, timeout=3600,
                          encoding="utf-8", errors="replace")
    identical = proc.returncode == 0 and "identical" in proc.stdout
    if not identical:
        log((proc.stdout + proc.stderr)[-3000:])
    if unplaced:
        log(f"{len(unplaced)} names left out: mgbdis decodes no instruction at their address ({', '.join(unplaced[:8])})")
    if left_out:
        log(f"{len(left_out)} documented routines left as mgbdis has them: "
            + "; ".join(f"{n}: {why}" for n, why in left_out[:6]))
    return {"path": str(out), "identical": identical, "matched": matched, "functions": len(rows),
            "documented": len(entries), "left_out": left_out,
            "fallback": [(n, "mgbdis decodes no instruction at its address: not named") for n in unplaced],
            "log": (proc.stdout + proc.stderr)[-3000:]}


def _documented(net, proj, rows, log, judge=gb_asm) -> tuple[list, list[tuple[str, str]]]:
    """The routines documented in assembly that go into the build, and the ones that don't, with why. Each is judged
    again with the project's names as they are now: the six checks, the shift test among them, over the whole ROM's
    names (one whose names changed under it since, or that no longer rebuilds, stays as the disassembly has it).
    `judge`: rom/gb_asm.py (RGBDS) or rom/sms_asm.py (WLA-DX)."""
    todo = [r for r in rows if r["status"] == "documented" and r["matched_source"] and r["rom_offset"] is not None]
    if not todo:
        return [], []
    if not judge.available():
        return [], [(r["name"], f"{'WLA-DX' if judge is not gb_asm else 'RGBDS'} isn't installed") for r in todo]
    log(f"judging the {len(todo)} documented routines again with the project's names as they are now")
    routines = [(r, net._routine(proj, r)) for r in todo]

    def check(item):
        r, routine = item
        try:
            v = judge.judge(routine, r["matched_source"])
        except judge.AsmError as e:
            return str(e)
        return None if v.ok else (v.problems[0] if v.problems else "it doesn't pass")
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        verdicts = list(pool.map(check, routines))
    kept = [r for r, why in zip(todo, verdicts) if why is None]
    return kept, [(r["name"], why) for r, why in zip(todo, verdicts) if why is not None]


def _write_documented(net, proj, out: Path, rows, labels: set[str]) -> list[dict]:
    """documented/: each routine's text, and names.inc with the project's names they use that aren't labels in the
    build (RAM, constants); include/: hardware.inc 6.0 (CC0) and its older names, which they're written with."""
    if not rows:
        return []
    (out / DOCUMENTED).mkdir()
    entries, used, own = [], set(), set()
    for r in rows:
        bank, text = r["rom_offset"] // BANK_SIZE, r["matched_source"].strip() + "\n"
        (out / DOCUMENTED / f"{r['name']}.asm").write_text(text, encoding="utf-8")
        entries.append({"name": r["name"], "bank": bank, "address": r["vram"], "size": r["size"],
                        "file": f"{DOCUMENTED}/{r['name']}.asm"})
        _items, found, _header, _problems = gb_asm.parse(text)
        own |= {name for _n, name in found if "." not in name}
        for line in text.splitlines():
            used |= set(WORD.findall(gb_asm._split_comment(line)[0]))
    names = {r["name"]: r["address"] for r in net.store.all(
        "SELECT name, address FROM symbols WHERE project_id=? AND address IS NOT NULL", proj["id"])}
    names |= {r["name"]: r["vram"] for r in net.store.all(
        "SELECT name, vram FROM functions WHERE project_id=? AND vram IS NOT NULL", proj["id"])}
    hw = gb_asm.hardware_names()
    defs = sorted((n, v) for n, v in names.items() if n in used and n not in labels and n not in own and n not in hw)
    (out / DOCUMENTED / "names.inc").write_text(
        "; The project's names the documented routines use that aren't labels in the build (RAM, constants, ...)\n"
        + "".join(f"DEF {n} EQU ${v & 0xFFFF:04X}\n" for n, v in defs), encoding="utf-8")
    (out / "include").mkdir()
    for f in ("hardware.inc", "hardware_compat.inc"):
        shutil.copyfile(gb_hw.include_dir() / f, out / "include" / f)
    (out / "include" / "old_names.inc").write_text(gb_hw.old_names_inc(), encoding="utf-8")
    return entries


def _blocks(rows, regions, size: int, documented=()) -> list[str]:
    """mgbdis blocks for the project's split: a `.code` block at every function start (up to the next start or data
    region in its bank) and a `.data` block for every data region, so the disassembly has the split's layout. A
    documented routine ((ROM offset, size) in `documented`) is one `.data` block that nothing else starts in: mgbdis
    writes its bytes there, which build.py swaps for its text."""
    cuts = sorted(documented)

    def inside(flat: int) -> bool:
        k = bisect.bisect_right(cuts, (flat, 1 << 30)) - 1
        return k >= 0 and cuts[k][0] <= flat < cuts[k][0] + cuts[k][1]
    placed = sorted((r for r in rows if r["rom_offset"] is not None), key=lambda r: r["rom_offset"])
    rows = [r for k, r in enumerate(placed) if not 0x104 <= r["rom_offset"] < 0x150
            and not (k and _mid_instruction(r, placed[k - 1])) and not inside(r["rom_offset"])]
    spans = []  # the data regions, less what documented routines cover
    for r in regions:
        a, b = r["rom_offset"], r["rom_offset"] + r["size"]
        for c, n in cuts:
            if c < b and a < c + n:
                if a < c:
                    spans.append((a, c))
                a = max(a, c + n)
        if a < b:
            spans.append((a, b))
    bounds = sorted({r["rom_offset"] for r in rows} | {a for a, _b in spans} | {b for _a, b in spans}
                    | {c for c, _n in cuts} | {c + n for c, n in cuts})
    out = []
    for r in rows:
        flat = r["rom_offset"]
        k = bisect.bisect_right(bounds, flat)
        end = min(bounds[k] if k < len(bounds) else size, (flat // BANK_SIZE + 1) * BANK_SIZE)
        out.append(f"{flat // BANK_SIZE:02x}:{r['vram'] or 0:04x} .code:{end - flat:x}")
    for a, b in spans + [(c, c + n) for c, n in cuts]:
        flat, n = a, b - a
        while n > 0:  # a block stays in its bank
            take = min(n, (flat // BANK_SIZE + 1) * BANK_SIZE - flat)
            cpu = flat if flat < BANK_SIZE else BANK_SIZE + flat % BANK_SIZE
            out.append(f"{flat // BANK_SIZE:02x}:{cpu:04x} .data:{take:x}")
            flat, n = flat + take, n - take
    return out


def _mid_instruction(r, prev) -> bool:
    """Whether a function starts inside an instruction of the one before it (an older split's guess): a block there
    would cut that instruction in two, so it gets none (and its name, no label)."""
    flat = r["rom_offset"]
    if prev["rom_offset"] + (prev["size"] or 0) <= flat:
        return False
    code = bytes(prev["target"] or b"")
    starts = {a for a, _t, _ln in sm83.disassemble(code, prev["rom_offset"])}
    return flat not in starts


def _docs(out: Path, title: str, base: str, matched: int, total: int, documented: int = 0) -> None:
    (out / "README.md").write_text(f"""# {title} (disassembly)

The game as assembly that rebuilds it byte for byte, with the names and notes worked out with
[decomp.wtf](https://decomp.wtf). Most Game Boy games were written in assembly: this is their source, the base for
mods, translations and ports. {documented} of the {total} functions found so far are documented in assembly (in
`documented/`), and {matched} are also matched in C (in `c/`).

## Build

1. Your own copy of the game is here as `{base}` (it is in `.gitignore`; never commit it).
2. `python build.py`: it disassembles your copy with [mgbdis](https://github.com/mattcurrie/mgbdis) into
   `build/disassembly/` with the names in `baserom.sym` and the comments in `comments.txt`, builds it with
   [RGBDS](https://rgbds.gbdev.io), and checks the result against your copy. Needs decomp.wtf installed (it pins
   mgbdis and RGBDS), or RGBDS on `PATH` and `MGBDIS` set to the path of `mgbdis.py`.

## Layout

| Path | What it is |
| :-- | :-- |
| `baserom.sym` | Where every function starts (`.code:length`), what isn't code (`.data:length`: tables, padding, bytes the code never reaches) and every function's name (`bank:address Name`). mgbdis reads it; add labels and `.data`/`.text`/`.image` blocks here. |
| `comments.txt` | What's known about each routine (`Name: text`), put above it in the disassembly. |
| `documented/` | Routines documented in assembly: names, comments and constants for what the disassembly has as numbers, each checked to rebuild its bytes. `build.py` puts each in place of its bytes; `names.inc` holds the names they use that aren't labels (RAM, constants). `python build.py --no-documented` builds without them. |
| `include/` | [hardware.inc](https://github.com/gbdev/hardware.inc) 6.0 (CC0) and its older names, which the documented routines are written with. |
| `c/` | Functions matched in C: SDCC (GBDK-2020) compiles each to exactly the original code. For reference: the build is the assembly. |
| `build.py`, `build.json` | The build, and which game it's for. |

`build/disassembly/` is made from your copy on every build, so keep your work in `baserom.sym`, `comments.txt` and
`documented/`.
To change the game itself, copy `build/disassembly/` elsewhere and edit it there: its `Makefile` builds it with RGBDS
and lets rgbfix write the header's new checksums.

The game's code and data belong to their rights holder, and the documented routines are its code written out:
keep this tree to yourself unless you have the right to share it.
""", encoding="utf-8")
