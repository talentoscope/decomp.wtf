"""Export a Master System or Game Gear project as a WLA-DX project that rebuilds the game byte for byte.

As the Game Boy's (export_gb.py): most of these games are assembly, so this is their source, the base for mods,
translations and ports. The tree holds the project's work, never the game's bytes: where each function starts, runs
and ends and its name (layout.json), what's known about each routine (comments.txt), the routines documented in
assembly (documented/, with names.inc for the names they use), the matched functions' C (c/, for reference), the
hardware's names (include/hardware.inc) and the Z80 disassembler (tools/z80.py, decomp.wtf's own). build.py makes the
disassembly from the user's copy (code as instructions, the rest as bytes, a label at every function and every jump
target), puts each documented routine in place of its bytes, assembles it with WLA-DX and checks it against the copy.
"""

# Portions ported from SynapticDreams; Copyright (c) 2026 Sam Wolfs; MIT License (see licenses/synaptic-sms-MIT.txt).
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from ..limits import capped
from . import sms, sms_asm, sms_hw
from .export_gb import NOTE_CHARS, NOTES_PER_LABEL, _documented

LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # a name WLA-DX takes as a label
WORD = re.compile(r"(?<![.@\w])[A-Za-z_]\w*")
DOCUMENTED = "documented"


def export_sms(net, slug: str, out: Path, log=print) -> dict:
    proj = net._project(slug)
    cfg = net.cfg(proj)
    rom_path = Path(proj["root"]) / cfg["rom"]["file"]
    image = sms.strip_copier_header(rom_path.read_bytes())
    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    platform = cfg.get("platform") or "sms"
    base = f"baserom.{'gg' if platform == 'gg' else 'sms'}"

    rows = net.store.all("SELECT * FROM functions WHERE project_id=? AND rom_offset IS NOT NULL ORDER BY rom_offset",
                         proj["id"])
    regions = net.store.all("SELECT * FROM data_regions WHERE project_id=? ORDER BY rom_offset", proj["id"])
    documented, left_out = _documented(net, proj, rows, log, judge=sms_asm)
    hw = set(sms_hw.names())
    named = [r for r in rows if LABEL.match(r["name"]) and r["name"] not in hw]
    named_ids, documented_ids = {r["id"] for r in named}, {r["id"] for r in documented}
    notes, matched = [], 0
    for r in named:
        why = json.loads(r["meta"] or "{}").get("asm_code") or ""
        said = [f"Set aside: {why}"] if why and r["status"] == "asm" else []
        if r["status"] == "matched" and r["matched_source"]:
            (out / "c").mkdir(exist_ok=True)
            (out / "c" / f"{r['name']}.c").write_text(
                f"/* {r['name']} (bank {(r['rom_offset']) // sms.BANK_SIZE}, ${r['vram'] or 0:04x}): SDCC (GBDK-2020, "
                f"flags {' '.join(cfg['compiler']['flags'])}) compiles this to exactly the original code, with context.h."
                f" */\n#include \"context.h\"\n\n{r['matched_source'].strip()}\n", encoding="utf-8")
            said.append(f"Matched in C: c/{r['name']}.c")
            matched += 1
        if r["id"] in documented_ids:
            said.append(f"Documented in assembly: {DOCUMENTED}/{r['name']}.asm (build.py puts it in place of its bytes)")
        for n in net.store.all("SELECT text FROM notes WHERE function_id=? ORDER BY id DESC LIMIT ?", r["id"], NOTES_PER_LABEL):
            text = " ".join(n["text"].split())
            said.append(text if len(text) <= NOTE_CHARS else text[:NOTE_CHARS - 3] + "...")
        notes += [f"{r['name']}: {line}" for text in said for line in textwrap.wrap(text, 110)]
    (out / "comments.txt").write_text("\n".join(notes) + ("\n" if notes else ""), encoding="utf-8")
    if matched:
        (out / "c" / "context.h").write_text(net._context(proj["id"])["header"], encoding="utf-8")

    entries = _write_documented(net, proj, out, [r for r in documented if LABEL.match(r["name"])],
                                {r["name"] for r in named})
    banks = {int(s["rom_start"]) // sms.BANK_SIZE: int(s["vram"]) for s in cfg.get("segments") or []}
    layout = {"functions": [{"name": r["name"] if r["id"] in named_ids else None, "flat": r["rom_offset"], "cpu": r["vram"],
                             "size": r["size"]} for r in rows],
              "regions": [[r["rom_offset"], r["size"], r["kind"]] for r in regions],
              "banks": {str(b): v for b, v in sorted(banks.items())}, "documented": entries}
    (out / "layout.json").write_text(json.dumps(layout, indent=0), encoding="utf-8")
    (out / "include").mkdir(exist_ok=True)
    (out / "include" / "hardware.inc").write_text(
        "; The hardware's names (decomp.wtf's fixed set for the Master System and Game Gear: rom/sms_hw.py)\n"
        + sms_hw.inc(), encoding="utf-8")
    (out / "tools").mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("z80.py"), out / "tools" / "z80.py")
    (out / "build.json").write_text(json.dumps({"title": proj["name"], "platform": platform, "image": base,
                                                "sha1": hashlib.sha1(image).hexdigest(), "size": len(image)},
                                               indent=1), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("export_sms_build.py"), out / "build.py")
    license_dir = out / "licenses"
    license_dir.mkdir(exist_ok=True)
    license_file = Path(__file__).resolve().parents[3] / "licenses" / "synaptic-sms-MIT.txt"
    shutil.copyfile(license_file, license_dir / license_file.name)
    (out / base).write_bytes(image)
    (out / ".gitignore").write_text(f"build/\n{base}\n__pycache__/\n", encoding="utf-8")
    _docs(out, proj["name"], base, matched, len(rows), len(entries))

    log("building the exported project to check it rebuilds the game byte for byte")
    proc = subprocess.run(capped([sys.executable, "build.py"]), cwd=out, capture_output=True, text=True, timeout=3600,
                          encoding="utf-8", errors="replace")
    identical = proc.returncode == 0 and "identical" in proc.stdout
    if not identical:
        log((proc.stdout + proc.stderr)[-3000:])
    for name, why in proc.stdout and re.findall(r"^left as bytes: (\S+): (.+)$", proc.stdout, re.M) or []:
        left_out.append((name, why))
    if left_out:
        log(f"{len(left_out)} documented routines left as the disassembly has them: "
            + "; ".join(f"{n}: {why}" for n, why in left_out[:6]))
    return {"path": str(out), "identical": identical, "matched": matched, "functions": len(rows),
            "documented": len(entries) - sum(1 for n, _ in left_out if any(e["name"] == n for e in entries)),
            "left_out": left_out, "fallback": [], "log": (proc.stdout + proc.stderr)[-3000:]}


def _write_documented(net, proj, out: Path, rows, labels: set[str]) -> list[dict]:
    """documented/: each routine's text, and names.inc with the project's names they use that aren't labels in the
    build (RAM, constants)."""
    if not rows:
        return []
    (out / DOCUMENTED).mkdir()
    entries, used, own = [], set(), set()
    for r in rows:
        text = r["matched_source"].strip() + "\n"
        (out / DOCUMENTED / f"{r['name']}.asm").write_text(text, encoding="utf-8")
        entries.append({"name": r["name"], "flat": r["rom_offset"], "cpu": r["vram"], "size": r["size"],
                        "file": f"{DOCUMENTED}/{r['name']}.asm"})
        _items, found, _header, _problems = sms_asm.parse(text)
        own |= {name for _n, name in found if LABEL.match(name)}
        for line in text.splitlines():
            used |= set(WORD.findall(sms_asm._split_comment(line)[0]))
    names = {r["name"]: r["address"] for r in net.store.all(
        "SELECT name, address FROM symbols WHERE project_id=? AND address IS NOT NULL", proj["id"])}
    hw = set(sms_hw.names())
    defs = sorted((n, v) for n, v in names.items() if n in used and n not in labels and n not in own and n not in hw
                  and LABEL.match(n))
    (out / DOCUMENTED / "names.inc").write_text(
        "; The project's names the documented routines use that aren't labels in the build (RAM, constants, ...)\n"
        + "".join(f".DEFINE {n} ${v & 0xFFFF:04X}\n" for n, v in defs), encoding="utf-8")
    return entries


def _docs(out: Path, title: str, base: str, matched: int, total: int, documented: int) -> None:
    (out / "README.md").write_text(f"""# {title} (disassembly)

The game as WLA-DX assembly that rebuilds it byte for byte, with the names and notes worked out with
[decomp.wtf](https://decomp.wtf). Most Master System and Game Gear games were written in assembly: this is their
source, the base for mods, translations and ports. {documented} of the {total} functions found so far are documented
in assembly (in `documented/`), and {matched} are also matched in C (in `c/`).

## Build

1. Your own copy of the game is here as `{base}` (it is in `.gitignore`; never commit it).
2. `python build.py`: it makes the disassembly from your copy into `build/disassembly/` (code as instructions with
   the project's names, the rest as bytes, the notes in `comments.txt` as comments, each documented routine in place
   of its bytes), builds it with [WLA-DX](https://github.com/vhelin/wla-dx), and checks the result against your
   copy. Needs WLA-DX: decomp.wtf's pinned one when decomp.wtf is installed, else `wla-z80` and `wlalink` on `PATH`
   (or `WLA_DX` set to the folder that has them).

## Layout

| Path | What it is |
| :-- | :-- |
| `layout.json` | Where each function starts, the address it runs at, its size and its name; what isn't code; where each bank runs. |
| `comments.txt` | What's known about each routine (`Name: text`), put above it in the disassembly. |
| `documented/` | Routines documented in assembly: names, comments and constants for what the disassembly has as numbers, each checked to rebuild its bytes. `build.py` puts each in place of its bytes; `names.inc` holds the names they use that aren't labels (RAM, constants). `python build.py --no-documented` builds without them. |
| `include/hardware.inc` | The hardware's names the documented routines use (ports, the mapper's registers). |
| `c/` | Functions matched in C: SDCC compiles each to exactly the original code. For reference: the build is the assembly. |
| `tools/z80.py`, `build.py`, `build.json` | The disassembler, the build, and which game it's for. |

`build/disassembly/` is made from your copy on every build, so keep your work in `layout.json`, `comments.txt` and
`documented/`. To change the game itself, copy `build/disassembly/` elsewhere and edit it there (`game.asm` is the
file to assemble); the header's checksum then needs updating, which WLA-DX's `.COMPUTESMSCHECKSUM` does.

The game's code and data belong to their rights holder, and the documented routines are its code written out:
keep this tree to yourself unless you have the right to share it.
""", encoding="utf-8")
