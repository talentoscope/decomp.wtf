"""Game Boy split by mgbdis + rgbds, with the code told from the data, and verifiable.

The built-in split (`gb.functions`) walks the ROM with the decoder and cuts functions at call targets.
That is a guess. [mgbdis](https://github.com/mattcurrie/mgbdis) writes RGBDS source whose output, assembled by
[rgbds](https://github.com/gbdev/rgbds), **rebuilds the ROM byte for byte**. That reassembly check is
what makes this split trustworthy: if the disassembly did not cover the ROM exactly, the rebuild would
differ, and we report that instead of pretending.

mgbdis itself decodes every byte as code unless its symbol file says otherwise (graphics come out as instructions,
the `call`-like bytes in them as labels, and those as functions). So the game's code is followed first with the banks
it maps (rom/gb_trace.py), and mgbdis is handed what that found: a `.code` block and a label at every function start
it reached, and a `.data` block for tables, padding and what the code never reached and reads as no code would. A
symbol file next to the ROM (`rom.sym`: pret, BGB and Emulicious write them) comes after, so its names and blocks win.
When that disassembly doesn't rebuild the ROM, the plain one is used instead.

Pipeline (all in a temp dir):

    mgbdis.py rom.gbc --output-dir out      # out/bank_XXX.asm + game.asm + Makefile (reads rom.sym)
    rgbasm -o game.o game.asm               # assemble every bank
    rgblink -n game.sym -o game.gbc game.o  # link, writing the symbol file
    rgbfix -p 255 game.gbc                  # the padding a released cartridge has (not -v: the header's checksums
                                            # are in the disassembly as the ROM has them, right or wrong)
    cmp game.gbc <rom>                      # faithful? (reported, and required for confidence)

`game.sym` is `bank:address Name` per line; those labels are the function starts, with the bytes read
from the original ROM (the rebuild is only the proof, never the source of truth).
"""

from __future__ import annotations

import bisect
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..handwritten import sm83_reason, sm83_routine
from . import gb, sm83
from .n64 import RomError

SYM = re.compile(r"^([0-9A-Fa-f]{2}):([0-9A-Fa-f]{4})\s+(\S+)")
LABEL_LINE = re.compile(r"^([A-Za-z_.][\w.$]*?)::?\s*$")
#: What a **function entry** looks like in mgbdis's output: something a `call` reaches (`Call_`/`Func_`),
#: a reset vector (`RST_`) or an interrupt vector. `Jump_` and `jr_` labels are *internal branch targets*
#: — mgbdis emits tens of thousands of them (26,709 `jr_` and 1,511 `Jump_` on a 1 MiB cartridge) and
#: treating them as boundaries cuts functions into one-byte pieces. Data labels are excluded too.
FUNCTION_PREFIXES = ("Func_", "Call_", "RST_")
INTERRUPT_SUFFIX = "Interrupt"
DATA_PREFIXES = ("Data_", "Unknown_", "Gfx_", "Pal_", "Map_", "Text_", "String_", "Ptr_", "Table_",
                 "Tileset_", "Tilemap_", "Music_", "SFX_", "Obj_", "Header")
MAX_FUNCTION = 0x400
HEADER_START = 0x104  # the cartridge header (logo, title, checksums) up to 0x150: data, in mgbdis's own symbols


@dataclass
class ToolSet:
    python: str
    mgbdis: Path
    rgbasm: Path
    rgblink: Path
    rgbfix: Path


def _tool(tid: str, name: str) -> Path | None:
    """A binary from an installed toolchain (any layout: the fork's installer, a nested archive, or a
    system install) — the same discovery rule gb_build uses for GBDK."""
    try:
        from .. import toolchains as tc

        roots = [Path(tc.path(tid))]
    except Exception:
        roots = []
    roots += [Path.home() / "Library/Application Support/decompnet/toolchains" / tid,
              Path.home() / ".local/share/decompnet/toolchains" / tid]
    for root in roots:
        if not root.is_dir():
            continue
        for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
            for sub in (cand / "bin", cand):
                exe = sub / name
                if exe.exists():
                    return exe
                # On Windows, executables have .exe extension; Path.exists() doesn't auto-append it.
                if sys.platform == "win32":
                    exe_exe = sub / (name + ".exe")
                    if exe_exe.exists():
                        return exe_exe
    return None


def tools() -> ToolSet | None:
    """mgbdis + rgbds, or None when they are not installed (the caller falls back to the built-in split)."""
    import sys

    # The released source archive nests itself (mgbdis-3.0/mgbdis.py), a checkout may not: accept both.
    roots: list[Path] = []
    try:
        from .. import toolchains as tc

        roots.append(Path(tc.path("mgbdis")))
    except Exception:
        pass
    roots += [Path.home() / "Library/Application Support/decompnet/toolchains/mgbdis",
              Path.home() / ".local/share/decompnet/toolchains/mgbdis"]
    mgbdis_dir = None
    for root in roots:
        if not root.is_dir():
            continue
        for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
            if (cand / "mgbdis.py").exists():
                mgbdis_dir = cand
                break
        if mgbdis_dir:
            break
    rgbasm, rgblink = _tool("rgbds", "rgbasm"), _tool("rgbds", "rgblink")
    if mgbdis_dir is None or rgbasm is None or rgblink is None:
        # a system rgbds (Homebrew, a distro package) is fine too
        rgbasm = rgbasm or (Path(p) if (p := shutil.which("rgbasm")) else None)
        rgblink = rgblink or (Path(p) if (p := shutil.which("rgblink")) else None)
        if mgbdis_dir is None or rgbasm is None or rgblink is None:
            return None
    rgbfix = _tool("rgbds", "rgbfix") or (Path(p) if (p := shutil.which("rgbfix")) else None)
    return ToolSet(python=sys.executable, mgbdis=mgbdis_dir / "mgbdis.py", rgbasm=rgbasm,
                   rgblink=rgblink, rgbfix=rgbfix or rgblink.with_name("rgbfix"))


def available() -> bool:
    return tools() is not None


def _run(argv: list, cwd: Path, timeout: float = 900.0) -> tuple[int, str]:
    try:
        res = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except FileNotFoundError as e:
        return 127, str(e)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


# ------------------------------------------------------------------ pure parsing helpers

def parse_sym(text: str) -> list[tuple[int, int, str]]:
    """(bank, address, name) for every label in an rgblink symbol file (its header comment is skipped)."""
    out = []
    for line in text.splitlines():
        m = SYM.match(line.strip())
        if m:
            out.append((int(m.group(1), 16), int(m.group(2), 16), m.group(3)))
    return out


def is_code(name: str) -> bool:
    """Whether a symbol is a **function entry** (so it bounds a function), rather than an internal branch
    target, a local label, or data."""
    if "." in name:  # a local label inside another function
        return False
    if name.startswith(DATA_PREFIXES):
        return False
    if name.startswith(FUNCTION_PREFIXES):
        return True
    return name.endswith(INTERRUPT_SUFFIX) or name in ("Boot", "EntryPoint", "Start", "Main")


def blocks(text: str) -> dict[str, str]:
    """{label: its assembly lines} out of one mgbdis bank file (RGBDS syntax, kept for the brief)."""
    out: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        m = LABEL_LINE.match(line)
        if m:
            if current is not None:
                out[current] = "\n".join(body).strip("\n")
            current, body = m.group(1), []
            continue
        if current is not None and line.strip() and not line.startswith(("SECTION", "INCLUDE", ";")):
            body.append(line.rstrip())
    if current is not None:
        out[current] = "\n".join(body).strip("\n")
    return out


def _end(data: bytes, bank: int, addr: int, hard: int) -> int:
    """Where the routine at `addr` ends by its own code: its first return or jump that no earlier branch goes past
    (the walk the Master System lane uses), never past `hard`."""
    limit, off = addr, addr
    while off < hard:
        flat = gb.flat_offset(off, bank)
        text, ln = sm83.decode(data[flat:flat + 3], off)
        if text.startswith("db "):
            return hard
        kind, target = sm83.flow(text)
        if target is not None and kind in ("jump", "cjump") and off < target < hard:
            limit = max(limit, target)
        off += ln
        if kind in ("ret", "jump") and off > limit:
            return off
    return hard


def _tails(data: bytes, bank: int, items: list[tuple[int, str]], named: dict, bounds: list[int]) -> list[tuple[int, str]]:
    """`items` (a bank's function starts) and the routines right after a function's own end that have a label there
    (something jumps to them, a tail call: mgbdis calls them `Jump_`), as functions of their own."""
    out = list(items)
    starts = {a for a, _ in out}
    k = 0
    while k < len(out):
        addr = out[k][0]
        nxt = out[k + 1][0] if k + 1 < len(out) else (0x8000 if bank else 0x4000)
        j = bisect.bisect_right(bounds, addr)
        hard = min(nxt, bounds[j]) if j < len(bounds) else nxt
        end = _end(data, bank, addr, hard)
        if end < hard and end not in starts and (bank, end) in named:
            out.insert(k + 1, (end, named[(bank, end)]))
            starts.add(end)
        k += 1
    return out


def _trim_padding(code: bytes) -> bytes:
    """Drop a run of 0x00/0xFF padding at the end: labels cover data regions too, and padding is not
    part of the function."""
    end = len(code)
    while end and code[end - 1] in (0x00, 0xFF):
        end -= 1
    return code[:end] if end else code


def _disassemble(rom_path: Path, tmp: Path, log=lambda *_: None) -> tuple[Path, list[tuple[int, int, str]], bool, bytes]:
    """mgbdis's disassembly of a ROM in `tmp`, assembled and linked with RGBDS: (its folder, its labels as
    (bank, address, name), whether it rebuilds the ROM byte for byte, the rebuilt bytes). Raises RomError."""
    ts = tools()
    if ts is None:
        raise RomError("mgbdis and rgbds are not installed (Setup lists them; the app installs them when a game is imported)")
    data = Path(rom_path).read_bytes()
    out = tmp / "out"
    # No `-I`: mgbdis imports its sibling modules from its own directory (the script dir must stay on
    # sys.path), and running it with the script path does exactly that.
    rc, log_text = _run([ts.python, ts.mgbdis, rom_path, "--output-dir", out], tmp)
    if rc != 0 or not (out / "game.asm").exists():
        raise RomError(f"mgbdis could not disassemble the ROM: {log_text.strip()[-400:]}")
    log(f"mgbdis: disassembled {len(list(out.glob('bank_*.asm')))} banks")
    rc, log_text = _run([ts.rgbasm, "-o", "game.o", "game.asm"], out)
    if rc != 0 or not (out / "game.o").exists():
        raise RomError(f"rgbasm could not assemble the disassembly: {log_text.strip()[-400:]}")
    rc, log_text = _run([ts.rgblink, "-n", "game.sym", "-o", "game.gbc", "game.o"], out)
    if rc != 0 or not (out / "game.sym").exists():
        raise RomError(f"rgblink could not link the disassembly: {log_text.strip()[-400:]}")
    if ts.rgbfix.exists():
        _run([ts.rgbfix, "-p", "255", "game.gbc"], out)
    rebuilt = (out / "game.gbc").read_bytes() if (out / "game.gbc").exists() else b""
    faithful = rebuilt == data
    symbols = parse_sym((out / "game.sym").read_text(encoding="utf-8", errors="replace"))
    log(f"mgbdis: {len(symbols)} labels, reassembly {'byte-identical' if faithful else 'DIFFERS'}")
    return out, symbols, faithful, rebuilt


def labels(rom_path: Path, log=lambda *_: None, sym: str | None = None) -> set[tuple[int, int]]:
    """The (bank, address) of every label in mgbdis's disassembly of a ROM (with `sym` as its symbol file, else none):
    where it decodes code from, so where a name of the project's can go (at any other address, mgbdis would be
    mid-instruction). Raises RomError."""
    with tempfile.TemporaryDirectory(prefix="gb-mgbdis-") as tmp:
        tmp = Path(tmp)
        rom = tmp / f"rom{Path(rom_path).suffix}"
        shutil.copyfile(rom_path, rom)  # alone: a .sym beside the original would steer mgbdis
        if sym is not None:
            rom.with_suffix(".sym").write_text(sym, encoding="utf-8")
        _, symbols, _, _ = _disassemble(rom, tmp, log)
    return {(bank, addr) for bank, addr, _ in symbols}


def mapped(data: bytes, hints: dict | None = None, log=lambda *_: None):
    """(the symbol file for mgbdis, what following the code found, the regions that aren't code): rom/gb_trace."""
    from . import gb_trace

    found = gb_trace.follow(data, hints)
    regs = gb_trace.regions(data, found, hints)
    kinds: dict[str, int] = {}
    for _flat, n, kind, _why in regs:
        kinds[kind] = kinds.get(kind, 0) + n
    log(f"followed the code: {len(found.starts)} functions reached, {sum(found.code.values())} bytes of code; "
        + ", ".join(f"{n} bytes of {kind}" for kind, n in sorted(kinds.items())) + " shown as data")
    return gb_trace.sym_text(data, found, regs), found, regs


# ------------------------------------------------------------------ the split

def split(rom_path: Path, workdir: Path, log=lambda *_: None, hints: dict | None = None) -> tuple[list[gb.GbFunction], dict]:
    """Functions from mgbdis's split, with the reassembly check reported in the second element.

    Raises RomError when the tools are missing or the disassembler/assembler fails. The returned dict is
    `{"faithful": bool, "labels": n, "functions": n, "rebuilt_bytes": n, "regions": [(flat, size, kind, why)],
    "mapped": bool}`; `hints` are what a person or an agent established (rom/gb_trace.Tracer)."""
    rom_path = Path(rom_path)
    data = rom_path.read_bytes()
    if not gb.is_gb(data):
        raise RomError("this isn't a Game Boy or Game Boy Color ROM")
    sym, found, regs = mapped(data, hints, log)
    reached = {(flat // gb.BANK_SIZE, gb.cpu_address(flat)) for flat in found.starts}
    edges = found.edges
    own = rom_path.with_suffix(".sym")
    if own.is_file():  # a symbol file next to the ROM: after the map, so its names and blocks win
        sym += own.read_text(encoding="utf-8", errors="replace")
        log(f"with the names and blocks in {own.name}")
    with tempfile.TemporaryDirectory(prefix="gb-mgbdis-") as tmp:
        tmp = Path(tmp)
        rom = tmp / f"rom{rom_path.suffix}"
        shutil.copyfile(rom_path, rom)
        rom.with_suffix(".sym").write_text(sym, encoding="utf-8")
        out, symbols, faithful, rebuilt = _disassemble(rom, tmp, log)
        if not faithful:  # the map broke the rebuild (it shouldn't): the plain disassembly, as before
            log("the disassembly with the map didn't rebuild the ROM byte for byte: splitting without it")
            regs, reached, edges = [], set(), {}
            shutil.rmtree(out, ignore_errors=True)
            rom.with_suffix(".sym").unlink()
            if own.is_file():
                shutil.copyfile(own, rom.with_suffix(".sym"))
            out, symbols, faithful, rebuilt = _disassemble(rom, tmp, log)

        # asm blocks, per bank file, so the brief can show the real disassembly
        asm_blocks: dict[str, str] = {}
        for f in sorted(out.glob("bank_*.asm")):
            asm_blocks.update(blocks(f.read_text(encoding="utf-8", errors="replace")))

        by_bank: dict[int, list[tuple[int, str]]] = {}
        for bank, addr, name in symbols:
            if is_code(name) or ((bank, addr) in reached and "." not in name):  # (a symbol file's name for one)
                by_bank.setdefault(bank, []).append((addr, name))
        for bank, items in by_bank.items():  # one entry per address: prefer the call-reached name
            best: dict[int, str] = {}
            for addr, name in items:
                current = best.get(addr)
                if current is None or (name.startswith(("Func_", "Call_")) and not current.startswith(("Func_", "Call_"))):
                    best[addr] = name
            by_bank[bank] = sorted(best.items())
        named = {(bank, addr): name for bank, addr, name in symbols if "." not in name and not name.startswith(DATA_PREFIXES)}
        data_bounds: dict[int, list[int]] = {}
        for flat, n, _kind, _why in regs:
            data_bounds.setdefault(flat // gb.BANK_SIZE, []).append(gb.cpu_address(flat))
        for bank in by_bank:  # a routine only jumped to, right after another's end, is a function of its own
            by_bank[bank] = _tails(data, bank, by_bank[bank], named, sorted(data_bounds.get(bank, [])))

        data_at: dict[int, list[int]] = {0: [HEADER_START]}  # bank -> where its data starts (a function ends there)
        for flat, _n, _kind, _why in regs:
            data_at.setdefault(flat // gb.BANK_SIZE, []).append(gb.cpu_address(flat))
        for starts in data_at.values():
            starts.sort()
        fns: list[gb.GbFunction] = []
        for bank, items in sorted(by_bank.items()):
            items.sort()
            for i, (addr, name) in enumerate(items):
                nxt = items[i + 1][0] if i + 1 < len(items) else (0x8000 if bank else 0x4000)
                cut = data_at.get(bank, [])
                k = bisect.bisect_right(cut, addr)
                if k < len(cut):
                    nxt = min(nxt, cut[k])
                flat = gb.flat_offset(addr, bank)
                if flat >= len(data):
                    continue
                end = min(flat + min(max(1, nxt - addr), MAX_FUNCTION), len(data))
                code = _trim_padding(data[flat:end])
                if not code:
                    continue
                # a routine the next called address doesn't bound (padding, data or code with no entry of its own
                # follow it) ends at its own end, when that's a routine SDCC could write (handwritten.py)
                if sm83_reason(code, addr, capped=nxt - addr > MAX_FUNCTION) and (n := sm83_routine(code, addr)):
                    code = code[:n]
                fns.append(gb.GbFunction(name=name, vram=addr, rom=flat, bank=bank, code=code,
                                         n_insns=sum(1 for _ in sm83.disassemble(code, addr)),
                                         asm=asm_blocks.get(name, ""),
                                         refs=gb.refs_of(code, addr, bank, len(data)),
                                         edges={site - flat: to for site, to in edges.items()
                                                if flat <= site < flat + len(code)}))
        meta = {"faithful": faithful, "labels": len(symbols), "functions": len(fns),
                "rebuilt_bytes": len(rebuilt), "original_bytes": len(data), "regions": regs, "mapped": bool(regs)}
        return fns, meta
