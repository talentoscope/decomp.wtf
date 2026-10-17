"""Commodore 64 program containers and 6510 code discovery."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import capstone

from ..compiler import CompilerConfig, _exec, check_source
from .n64 import RomError

_D64_SECTORS = [0] + [21] * 17 + [19] + [18] * 7 + [17] * 6 + [17] * 9
_T64_MAGIC = (b"C64 tape image file", b"C64S tape image file")
_TAP_MAGIC = b"C64-TAPE-RAW"
_CRT_MAGIC = b"C64 CARTRIDGE   "
EXTENSIONS = (".prg", ".d64", ".t64", ".crt")
_SYS = re.compile(rb"\x9e\s*([0-9]{1,5})")
_BRANCHES = {"bcc", "bcs", "beq", "bmi", "bne", "bpl", "bvc", "bvs"}


@dataclass
class C64Function:
    name: str
    address: int
    code: bytes
    asm: str
    n_insns: int
    rom_offset: int


def _petascii(raw: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else " " for c in raw).strip()


def _prg(data: bytes, name: str) -> tuple[bytes, str]:
    if len(data) < 3:
        raise RomError("this C64 program is too short to contain a load address and code")
    address = int.from_bytes(data[:2], "little")
    if address + len(data) - 2 > 0x10000:
        raise RomError("this C64 program has an invalid load address or extends past the 6510 address space")
    return data, name or "C64 program"


def _d64(data: bytes) -> tuple[bytes, str]:
    if len(data) not in (174848, 175531, 196608, 197376):
        raise RomError("this D64 image has an unsupported or invalid disk size")
    stride = 257 if len(data) in (175531, 197376) else 256

    def sector(track: int, index: int) -> bytes:
        if not 1 <= track < len(_D64_SECTORS) or not 0 <= index < _D64_SECTORS[track]:
            raise RomError("the D64 directory or file chain points to an invalid sector")
        offset = (sum(_D64_SECTORS[1:track]) + index) * stride
        if offset + 256 > len(data):
            raise RomError("the D64 image ends before a referenced sector")
        return data[offset:offset + 256]

    track, index = 18, 1
    files = []
    visited = set()
    while track:
        if (track, index) in visited:
            raise RomError("the D64 directory contains a loop")
        visited.add((track, index))
        block = sector(track, index)
        for at in range(2, 256, 32):
            kind = block[at]
            if not kind & 0x07:
                continue
            name = _petascii(block[at + 3:at + 19].replace(b"\xa0", b" "))
            files.append((kind & 0x07, name, block[at + 1], block[at + 2]))
        track, index = block[0], block[1]
    entry = next((f for f in files if f[0] == 2), None)
    if entry is None:
        raise RomError("this D64 has no closed PRG file")
    _, name, track, index = entry
    content = bytearray()
    visited.clear()
    while track:
        if (track, index) in visited:
            raise RomError(f"the D64 file {name!r} contains a loop")
        visited.add((track, index))
        block = sector(track, index)
        next_track, next_index = block[0], block[1]
        content.extend(block[2:] if next_track else block[2:2 + max(0, next_index - 1)])
        track, index = next_track, next_index
    return _prg(bytes(content), name)


def _t64(data: bytes) -> tuple[bytes, str]:
    if len(data) < 0x40 or not any(data.startswith(magic) for magic in _T64_MAGIC):
        raise RomError("this T64 tape image has an invalid header")
    count = int.from_bytes(data[0x24:0x26], "little")
    for i in range(count):
        at = 0x40 + i * 32
        if at + 32 > len(data):
            raise RomError("the T64 directory is truncated")
        entry = data[at:at + 32]
        if entry[0] not in (1, 2):
            continue
        start = int.from_bytes(entry[2:4], "little")
        end = int.from_bytes(entry[4:6], "little")
        offset = int.from_bytes(entry[8:12], "little")
        size = end - start
        if not size or offset + size > len(data):
            continue
        name = _petascii(entry[16:32])
        return _prg(start.to_bytes(2, "little") + data[offset:offset + size], name)
    raise RomError("this T64 tape image contains no readable program")


def _crt(data: bytes) -> tuple[bytes, str]:
    if len(data) < 0x40 or data[:16] != _CRT_MAGIC:
        raise RomError("this CRT cartridge image has an invalid header")
    header_size = int.from_bytes(data[0x10:0x14], "big")
    if header_size < 0x40 or header_size > len(data):
        raise RomError("the CRT cartridge header has an invalid size")
    title = _petascii(data[0x20:0x40])
    at = header_size
    while at + 0x10 <= len(data):
        if data[at:at + 4] != b"CHIP":
            raise RomError("the CRT cartridge contains an invalid CHIP packet")
        packet_size = int.from_bytes(data[at + 4:at + 8], "big")
        load = int.from_bytes(data[at + 0x0C:at + 0x0E], "big")
        size = int.from_bytes(data[at + 0x0E:at + 0x10], "big")
        if packet_size < 0x10 or packet_size != 0x10 + size or at + packet_size > len(data):
            raise RomError("the CRT cartridge contains a truncated CHIP packet")
        if load + size <= 0x10000 and size:
            return _prg(load.to_bytes(2, "little") + data[at + 0x10:at + packet_size], title or "C64 cartridge")
        at += packet_size
    raise RomError("this CRT cartridge contains no supported CHIP data")


TAP_HELP = ("this is a C64 tape image (TAP), which decomp.wtf can't read directly. Extract the program to a PRG "
            "first with TapClean (command line) or TapClean Front End, \"tapcleanfe\" (GUI), then open the .prg: "
            "https://sourceforge.net/projects/tapclean/")


def read_program(data: bytes, name: str = "") -> tuple[bytes, dict]:
    """Return a normalized PRG plus metadata for PRG, D64, T64, or CRT input."""
    ext = Path(name).suffix.lower()
    if data.startswith(_TAP_MAGIC) or ext == ".tap":
        raise RomError(TAP_HELP)
    if data.startswith(_CRT_MAGIC) or ext == ".crt":
        program, title = _crt(data)
        kind = "CRT"
    elif any(data.startswith(magic) for magic in _T64_MAGIC) or ext == ".t64":
        program, title = _t64(data)
        kind = "T64"
    elif ext == ".d64" or len(data) in (174848, 175531, 196608, 197376):
        program, title = _d64(data)
        kind = "D64"
    elif ext == ".prg" or ext == ".p00":
        program, title = _prg(data, Path(name).stem)
        kind = "PRG"
    else:
        raise RomError("this isn't a C64 PRG, D64, T64 or CRT file")
    address = int.from_bytes(program[:2], "little")
    image = program[2:]
    return program, {
        "title": title,
        "game_code": "",
        "region": "",
        "size": len(image),
        "sha1": hashlib.sha1(program).hexdigest(),
        "entrypoint": entrypoint(image, address),
        "platform": "c64",
        "byte_order": "little",
        "compiler_guess": "CC65",
        "load_address": address,
        "container": kind,
        "program_size": len(program),
    }


def entrypoint(image: bytes, base: int) -> int:
    """Find a BASIC SYS statement when present; otherwise use the PRG load address."""
    if base != 0x0801:
        return base
    at = 0
    while at + 4 <= len(image):
        next_line = int.from_bytes(image[at:at + 2], "little")
        if not next_line:
            break
        end = image.find(b"\0", at + 4)
        if end < 0:
            break
        match = _SYS.search(image[at + 4:end])
        if match:
            address = int(match.group(1))
            if base <= address < base + len(image):
                return address
        at = next_line - base
        if not 0 <= at < len(image):
            break
    return base


def split(program: bytes, log=lambda *_: None, entries: list[int] | None = None,
          outside: set[int] | None = None) -> list[C64Function]:
    """Find reachable 6510 routines from a PRG entry and emit assembler suitable for matching.
    Absolute jsr/jmp targets that fall outside this program are added to `outside`."""
    base = int.from_bytes(program[:2], "little")
    code = program[2:]
    md = capstone.Cs(capstone.CS_ARCH_MOS65XX, capstone.CS_MODE_MOS65XX_6502)
    md.detail = True
    md.skipdata = True
    decoded: dict[int, capstone.CsInsn | None] = {}

    def decode(pc: int):
        # decode from each reached address: jumps land mid-way through a linear sweep of the bytes
        if pc not in decoded:
            at = pc - base
            decoded[pc] = next(md.disasm(code[at:at + 3], pc), None) if 0 <= at < len(code) else None
        return decoded[pc]

    starts = entries or [entrypoint(code, base)]
    found: dict[int, tuple[set[int], set[int], bool]] = {}
    while starts:
        start = starts.pop(0)
        if start in found or decode(start) is None:
            continue
        seen: set[int] = set()
        calls: set[int] = set()
        ended = False
        todo = [start]
        while todo:
            pc = todo.pop()
            while pc not in seen and (ins := decode(pc)) is not None:
                if ins.mnemonic.startswith("."):
                    break
                seen.add(pc)
                nxt = pc + ins.size
                operand = ins.op_str.strip()
                match = re.fullmatch(r"(?:\$|0x)([0-9a-fA-F]{4})", operand)
                dest = int(match.group(1), 16) if match else None
                if outside is not None and dest is not None and ins.mnemonic in ("jsr", "jmp") \
                        and not base <= dest < base + len(code):
                    outside.add(dest)
                if ins.mnemonic == "jsr":
                    if dest is not None and base <= dest < base + len(code):
                        calls.add(dest)
                    pc = nxt
                elif ins.mnemonic in ("rts", "rti"):
                    ended = True
                    break
                elif ins.mnemonic == "brk":
                    break
                elif ins.mnemonic == "jmp":
                    ended = True
                    if dest is None or not base <= dest < base + len(code):
                        break
                    pc = dest
                elif ins.mnemonic in _BRANCHES:
                    if dest is not None and base <= dest < base + len(code):
                        todo.append(dest)
                    pc = nxt
                else:
                    pc = nxt
        found[start] = (seen, calls, ended)
        starts.extend(sorted(calls - found.keys()))

    ordered = sorted(found)
    out = []
    for index, start in enumerate(ordered):
        seen, calls, ended = found[start]
        # zero bytes (BRK) or a run that never returns, jumps or calls anything is data, not a routine
        if not seen or decode(start).mnemonic == "brk" or not (ended or calls):
            continue
        end = max(seen, default=start) + decoded[max(seen)].size
        if index + 1 < len(ordered):
            end = min(end, ordered[index + 1])
        if end <= start:
            continue
        raw = code[start - base:end - base]
        out.append(C64Function(f"func_{start:04X}", start, raw, listing(raw, start), len(seen),
                               start - base + 2))
    log(f"found {len(out)} reachable functions from 0x{entrypoint(code, base):04X}")
    return out


def listing(code: bytes, origin: int) -> str:
    md = capstone.Cs(capstone.CS_ARCH_MOS65XX, capstone.CS_MODE_MOS65XX_6502)
    md.skipdata = True
    lines = []
    cursor = 0
    for ins in md.disasm(code, origin):
        if ins.address > origin + cursor:
            lines.extend(".byte " + ", ".join(f"${b:02X}" for b in code[i:i + 8])
                         for i in range(cursor, ins.address - origin, 8))
        if ins.mnemonic.startswith(".") or (ins.mnemonic == "brk" and ins.size != 1):
            lines.append(".byte " + ", ".join(f"${b:02X}" for b in ins.bytes))
        else:
            operands = re.sub(r"0x([0-9a-fA-F]+)", r"$\1", ins.op_str).replace(", ", ",")
            lines.append(f"{ins.mnemonic} {operands}".rstrip())
        cursor = ins.address - origin + ins.size
    for at in range(cursor, len(code), 8):
        lines.append(".byte " + ", ".join(f"${b:02X}" for b in code[at:at + 8]))
    return "\n".join(lines)


def annotate(asm: str, names: dict[int, tuple[str, str]], unit: str = "") -> str:
    """Comment each jsr/jmp whose target is a known routine, naming the program when it is another block."""
    out = []
    for line in asm.splitlines():
        match = re.fullmatch(r"(jsr|jmp) \$([0-9A-Fa-f]{4})", line)
        target = names.get(int(match.group(2), 16)) if match else None
        if target:
            line += f"  ; {target[0]}" + (f" in {target[1]}" if target[1] != unit else "")
        out.append(line)
    return "\n".join(out)


def _assemble_line(assembler, line: str, pc: int) -> bytes:
    # py65 picks zero-page encodings for small values, but a four-digit operand like $0012 is absolute.
    absolute_zero_page = re.search(r"\$00([0-9a-fA-F]{2})(?![0-9a-fA-F])", line)
    if absolute_zero_page and line.split()[0].lower() not in _BRANCHES:
        widened = line[:absolute_zero_page.start()] + "$01" + absolute_zero_page.group(1) + \
            line[absolute_zero_page.end():]
        encoded = bytearray(assembler.assemble(widened, pc))
        if len(encoded) == 3:
            encoded[2] = 0
            return bytes(encoded)
    return bytes(assembler.assemble(line, pc))


def assemble(source: str, origin: int) -> bytes:
    """Assemble 6502 mnemonics and .byte directives at a function's original address."""
    try:
        from py65.assembler import Assembler
        from py65.devices.mpu6502 import MPU
    except ImportError as e:
        raise ValueError("the 6502 assembler dependency is missing; reinstall decompnet") from e

    assembler = Assembler(MPU())
    out = bytearray()
    pc = origin
    for line_no, raw in enumerate(source.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        if not line or line.endswith(":") or line.startswith(("*", ".org")):
            continue
        if line.lower().startswith(".byte"):
            values = line[5:].strip().split(",")
            try:
                encoded = bytes(int(value.strip().replace("$", "0x"), 0) for value in values)
            except ValueError as e:
                raise ValueError(f"line {line_no}: invalid .byte operand") from e
        else:
            try:
                encoded = _assemble_line(assembler, line, pc)
            except (ValueError, KeyError, IndexError, SyntaxError, OverflowError) as e:
                raise ValueError(f"line {line_no}: {e}") from e
        out.extend(encoded)
        pc += len(encoded)
        if pc > 0x10000:
            raise ValueError(f"line {line_no}: function extends past the 6510 address space")
    if not out:
        raise ValueError("no 6502 instructions or .byte values were provided")
    return bytes(out)


def _cc65_tool(tool: str) -> str | None:
    """A cc65 program from the toolchain decomp.wtf installed, else the one on PATH."""
    from .. import toolchains

    if toolchains.installed("cc65") is not None:
        for name in (f"{tool}.exe", tool):
            candidate = toolchains.path("cc65") / "bin" / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which(tool)


def compile_c(source: str, header: str, name: str, address: int, functions: dict[str, int]) -> bytes:
    """Compile a standalone C64 function with cc65 and link it at its original address."""
    try:
        check_source(source)
    except ValueError as e:
        raise ValueError(str(e)) from e
    tools = {tool: _cc65_tool(tool) for tool in ("cc65", "ca65", "ld65")}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        raise ValueError(f"C submissions need cc65 (missing: {', '.join(missing)}): install it from Compilers & tools, "
                         "or put cc65 on your PATH")
    symbol = "_" + name
    roots = {Path(path).resolve().parent.parent for path in tools.values()}
    libraries = [candidate for root in roots
                 for candidate in (root / "lib" / "c64.lib", root / "share" / "cc65" / "lib" / "c64.lib")
                 if candidate.is_file()]
    with tempfile.TemporaryDirectory(prefix="decompnet-c64-") as temp:
        work = Path(temp)
        (work / "func.c").write_text('#include "context.h"\n' + source, encoding="utf-8", newline="\n")
        (work / "context.h").write_text(header, encoding="utf-8", newline="\n")
        (work / "linker.cfg").write_text(
            f"""MEMORY {{
    MAIN: start = ${address:04X}, size = ${0x10000 - address:04X}, file = %O, fill = no;
    ZP: start = $0002, size = $00FE, type = rw, define = yes;
}}
SEGMENTS {{
    ZEROPAGE: load = ZP, type = zp;
    STARTUP: load = MAIN, type = ro, optional = yes;
    LOWCODE: load = MAIN, type = ro, optional = yes;
    ONCE: load = MAIN, type = ro, optional = yes;
    CODE: load = MAIN, type = ro, start = ${address:04X};
    RODATA: load = MAIN, type = ro, optional = yes;
    DATA: load = MAIN, type = rw, optional = yes;
    BSS: load = MAIN, type = bss, optional = yes;
}}
SYMBOLS {{
    __STACKSIZE__: type = weak, value = $0800;
"""
            + "".join(f"    _{sym}: type = weak, value = ${addr:04X};\n"
                      for sym, addr in sorted(functions.items()) if sym != name and re.fullmatch(r"[A-Za-z_]\w*", sym))
            + "}\n",
            encoding="utf-8",
        )
        extra_roots = tuple(str(root) for root in roots if not str(root).startswith("/usr"))
        cfg = CompilerConfig(cmd=[], timeout=30, ro_paths=extra_roots)

        def run(tool: str, *args: str) -> str:
            proc = _exec(cfg, work, [tools[tool], *args], cfg.timeout)
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode:
                raise ValueError(output[-3000:] or f"{tool} exited with {proc.returncode}")
            return output

        try:
            run("cc65", "-t", "c64", "-O", "-o", str(work / "func.s"), str(work / "func.c"))
            run("ca65", "-t", "c64", "-o", str(work / "func.o"), str(work / "func.s"))
            args = ["-C", str(work / "linker.cfg"), "-Ln", str(work / "labels.txt"), "-u", symbol,
                    "-o", str(work / "func.bin"), str(work / "func.o"), *map(str, libraries)]
            run("ld65", *args)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError(f"cc65 compilation failed: {e}") from e
        try:
            labels = (work / "labels.txt").read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ValueError(f"ld65 did not create its label output: {e}") from e
        label = re.search(rf"(?m)^(?:al|la)\s+([0-9A-Fa-f]{{4,6}})\s+\.*{re.escape(symbol)}\s*$", labels)
        if label is None:
            raise ValueError(f"ld65 did not export the function label {symbol}")
        linked_address = int(label.group(1), 16)
        if linked_address != address:
            raise ValueError(f"cc65 linked {name} at 0x{linked_address:04X}, not its target address 0x{address:04X}")
        binary = (work / "func.bin").read_bytes()
        if not binary:
            raise ValueError("ld65 produced an empty function")
        return binary
