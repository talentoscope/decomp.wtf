"""Load a C64 tape in VICE and take its RAM once loading has finished.

A tape's loader may be a turbo the TAP reader in ``c64`` cannot decode (Novaload, Freeload, ...). VICE can:
x64sc runs the tape in warp with its remote monitor on, and once the tape motor has stopped for good the full
64 KiB of RAM (``bank ram``, so the RAM under the ROMs and I/O) and the CPU registers are read back. Nothing
is profiled or traced: what's kept is one snapshot of memory, and the rest of the app analyses it statically.

The motor is bit 5 of the processor port ``$01`` (0 = on). Loading has finished when the motor has run and
then stayed off for ``quiet`` seconds, or when it never ran but the ROMs are banked out (VICE's tape traps
loaded the program at once). Nothing is typed: a prompt such as "HIT ANY KEY" stops the motor with tape still
left, and then the import stops and says so (the person gets past it in VICE and imports a snapshot instead).

How far the tape has played is worked out from VICE's cycle counter (the monitor's stopwatch) while the motor
runs, against the tape's length from its pulses: the datasette's own counter can't be read from the monitor.

VICE is found from ``DECOMP_VICE`` (x64sc itself, or the folder it is in), then on PATH. VICE is GPL and
only ever run as a separate program.

A VICE snapshot (``.vsf``) a person saved themselves can be read too (``read_vsf``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .n64 import RomError

VSF_MAGIC = b"VICE Snapshot File\x1a"
PAL_HZ = 985248  # the C64's clock (PAL), in cycles a second: the tape plays in step with it
# Tape still left when the motor stops for good, beyond which the stop is a prompt, not the end: the larger of
# these (the position is measured between polls, so it can be a little off)
LEFT_SECONDS, LEFT_SHARE = 15.0, 0.05


@dataclass
class Dump:
    ram: bytes
    regs: dict  # pc, a, x, y, sp, p00, p01 as ints
    screen: bytes | None = None  # a PNG of the screen when the RAM was taken, when VICE wrote one
    log: list[str] = field(default_factory=list)


def find_vice() -> Path | None:
    """x64sc: DECOMP_VICE (the program, or its folder), then the VICE decomp.wtf installed (Setup), then PATH."""
    names = ("x64sc.exe", "x64sc")
    given = os.environ.get("DECOMP_VICE", "").strip().strip('"')
    if given:
        p = Path(given)
        if p.is_file():
            return p
        for n in names:
            for q in (p / n, p / "bin" / n):
                if q.is_file():
                    return q
    from .. import toolchains

    try:
        if toolchains.installed("vice") is not None:
            return toolchains.binary("vice")
    except toolchains.ToolchainError:
        pass
    for n in names:
        found = shutil.which(n)
        if found:
            return Path(found)
    return None


def ensure_vice(log=print) -> Path | None:
    """x64sc, installing decomp.wtf's own VICE first when there's none and this computer has a build (Windows).
    None when there is no VICE and none can be installed: the tape is then read without it."""
    found = find_vice()
    if found is not None:
        return found
    from .. import toolchains

    if not toolchains.get("vice").supported:
        log("VICE isn't installed (install it from your package manager to read turbo tapes)")
        return None
    log("VICE isn't installed yet: installing it (47 MB, once)")
    toolchains.install("vice", log=log)
    return find_vice()


def tape_cycles(tap: bytes) -> int | None:
    """How long a TAP takes to play, in C64 cycles (None when it isn't a TAP)."""
    if tap[:12] != b"C64-TAPE-RAW" or len(tap) < 20:
        return None
    version, data, total, i = tap[12], tap[20:], 0, 0
    while i < len(data):
        b = data[i]
        if b:
            total, i = total + 8 * b, i + 1
        elif version >= 1 and i + 3 < len(data):
            total, i = total + int.from_bytes(data[i + 1:i + 4], "little"), i + 4
        else:
            total, i = total + 8 * 256, i + 1
    return total


def clock(cycles: float) -> str:
    """mm:ss of tape for a count of cycles."""
    secs = max(0, int(round(cycles / PAL_HZ)))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def stopped_early(played: float, total: int | None) -> bool:
    """True when the motor stopped for good with enough tape left that the program is waiting for something."""
    if not total:
        return False
    return total - played > max(LEFT_SECONDS * PAL_HZ, LEFT_SHARE * total)


class TapeClock:
    """The tape's position from the cycle counter: cycles only count while the motor ran at both readings (half,
    when it started or stopped in between)."""

    def __init__(self):
        self.played, self._last = 0.0, None

    def update(self, motor: bool, cycles: int) -> float:
        if self._last is not None:
            was, before = self._last
            if cycles >= before and (was or motor):
                self.played += (cycles - before) * (1.0 if was and motor else 0.5)
        self._last = (motor, cycles)
        return self.played


STOP_MESSAGE = ("The tape stopped at {at} of {length} and didn't start again, so the program is most likely waiting "
                "for a key or joystick press (a title screen, \"PRESS SPACE\", \"PRESS PLAY ON TAPE\" for the next "
                "part). decomp.wtf doesn't press anything for you. Load the tape in VICE yourself, get past the "
                "prompt, let it finish loading, then save a snapshot with VICE's snapshot menu (a .vsf file) and import "
                "that instead.")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Monitor:
    def __init__(self, port: int):
        self.port = port

    def send(self, cmds: list[str], timeout: float = 3.0) -> str:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        s.settimeout(0.5)
        out = b""

        def drain(wait: float) -> None:
            nonlocal out
            end = time.time() + wait
            while time.time() < end:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    if out:
                        break
                    continue
                if not chunk:
                    break
                out += chunk

        try:
            drain(0.5)
            for c in cmds:
                s.sendall(c.encode("ascii", "replace") + b"\n")
                drain(timeout)
            s.sendall(b"x\n")  # leave the monitor: the machine runs on
        finally:
            s.close()
        return out.decode("latin-1")

    def port01(self) -> int | None:
        m = re.search(r">C:0000\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})", self.send(["m 0000 0001"], 1.0))
        return int(m.group(2), 16) if m else None

    def state(self) -> tuple[int | None, int | None]:
        """($01, the cycle counter) from one register dump."""
        return parse_state(self.send(["r"], 1.0))


def parse_state(text: str) -> tuple[int | None, int | None]:
    """($01, stopwatch cycles) from the monitor's `r` output: `.;PC A X Y SP 00 01 NV-BDIZC LIN CYC  STOPWATCH`,
    the stopwatch being the last number on the line."""
    for line in text.splitlines():
        m = _REGS.search(line)
        if m:
            rest = re.findall(r"\d+", line[m.end():])
            return int(m.group(7), 16), (int(rest[-1]) if len(rest) >= 3 else None)
    return None, None


_REGS = re.compile(r"\.;([0-9a-f]{4}) ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2})")


def dump_tape(tap: bytes | Path, name: str = "tape.tap", log=print, quiet: float = 20.0, timeout: float = 900.0,
              vice: Path | None = None, every: float = 5.0) -> Dump:
    """Run a TAP in VICE until it has loaded, then take the RAM and the registers. Raises RomError when the tape
    stops for good with tape left (a prompt waiting for a key): see STOP_MESSAGE."""
    vice = vice or find_vice()
    if vice is None:
        raise RomError("VICE (x64sc) isn't found: install VICE and set DECOMP_VICE to x64sc or its folder")
    lines: list[str] = []
    raw = tap.read_bytes() if isinstance(tap, Path) else tap
    total = tape_cycles(raw)
    length = clock(total) if total else "?"

    def say(msg: str) -> None:
        lines.append(msg)
        if log:
            log(msg)

    with tempfile.TemporaryDirectory(prefix="decomp-c64-") as tmp:
        tmpd = Path(tmp)
        tap_path = tmpd / (Path(name).stem + ".tap")
        tap_path.write_bytes(raw)
        port = _free_port()
        mon = _Monitor(port)
        proc = subprocess.Popen([str(vice), "-default", "-warp", "-remotemonitor", "-remotemonitoraddress",
                                 f"ip4://127.0.0.1:{port}", "-autostart", str(tap_path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say(f"VICE: loading {Path(name).name} in warp (tape {length} long)")
        try:
            t0 = time.time()
            started, off_since, running_since, last = False, None, None, None
            tape, shown = TapeClock(), 0.0
            while True:
                if proc.poll() is not None:
                    raise RomError("VICE closed before the tape had loaded")
                if time.time() - t0 > timeout:
                    say(f"VICE: still loading after {timeout:.0f}s; taking the RAM as it is")
                    break
                time.sleep(1)
                try:
                    v, cycles = mon.state()
                except OSError:
                    continue
                if v is None:
                    continue
                motor = (v & 0x20) == 0
                played = tape.update(motor, cycles) if cycles is not None else tape.played
                where = f"tape {clock(played)} / {length}" + (f" ({min(100, 100 * played / total):.0f}%)" if total else "")
                if motor != last:
                    say(f"VICE: {time.time() - t0:5.0f}s tape motor {'on' if motor else 'off'} at {where}")
                    last, shown = motor, time.time()
                elif motor and time.time() - shown >= every:
                    say(f"VICE: {where}")
                    shown = time.time()
                stop = False
                if motor:
                    started, off_since, running_since = True, None, None
                elif started:
                    off_since = off_since or time.time()
                    stop = time.time() - off_since >= quiet
                elif (v & 7) != 7 or running_since:  # never ran, ROMs banked out: tape traps loaded it at once
                    running_since = running_since or time.time()
                    stop = time.time() - running_since >= 3 * quiet
                if stop:
                    if started and stopped_early(tape.played, total):
                        say(f"VICE: the tape stopped at {clock(tape.played)} of {length}; giving up")
                        raise RomError(STOP_MESSAGE.format(at=clock(tape.played), length=length))
                    break
            ram_file, png = tmpd / "ram.bin", tmpd / "screen.png"
            text = mon.send(["r", "bank ram", f'bsave "{ram_file}" 0 0000 ffff', "bank cpu",
                             f'screenshot "{png}" 2'], timeout=10)
            m = _REGS.search(text)
            if not m or not ram_file.is_file():
                raise RomError("VICE didn't hand back the RAM")
            regs = dict(zip(("pc", "a", "x", "y", "sp", "p00", "p01"), (int(g, 16) for g in m.groups())))
            ram = ram_file.read_bytes()
            if len(ram) != 0x10000:
                raise RomError(f"VICE saved {len(ram)} bytes of RAM, not 64 KiB")
            say(f"VICE: RAM taken at {time.time() - t0:.0f}s; PC ${regs['pc']:04X}, $01=${regs['p01']:02X}")
            return Dump(ram, regs, png.read_bytes() if png.is_file() else None, lines)
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def is_vsf(data: bytes) -> bool:
    return data.startswith(VSF_MAGIC)


def _modules(data: bytes):
    """(name, major, minor, body) for each module in a VICE snapshot."""
    pos = len(VSF_MAGIC) + 2 + 16  # magic, version, machine name
    if data[pos:pos + 12] == b"VICE Version":  # VICE 3.x: a version chunk before the modules
        pos += 13 + 4 + 4
    while pos + 22 <= len(data):
        name = data[pos:pos + 16].split(b"\0", 1)[0].decode("ascii", "replace")
        major, minor = data[pos + 16], data[pos + 17]
        size = int.from_bytes(data[pos + 18:pos + 22], "little")
        if size < 22 or pos + size > len(data):
            break
        yield name, major, minor, data[pos + 22:pos + size]
        pos += size


def read_vsf(data: bytes) -> Dump:
    """The RAM and registers in a C64 VICE snapshot (x64 or x64sc)."""
    if not is_vsf(data):
        raise RomError("this isn't a VICE snapshot")
    ram = regs = None
    for name, _major, _minor, body in _modules(data):
        if name == "C64MEM" and len(body) >= 4 + 0x10000:
            port, ddr = body[0], body[1]
            ram = bytes(body[4:4 + 0x10000])
        elif name == "MAINCPU" and len(body) >= 15:
            a, x, y, sp = body[8], body[9], body[10], body[11]
            regs = {"pc": int.from_bytes(body[12:14], "little"), "a": a, "x": x, "y": y, "sp": sp}
    if ram is None:
        # older layouts: fall back to finding the module by name
        i = data.find(b"C64MEM\0")
        if i < 0 or len(data) < i + 26 + 0x10000:
            raise RomError("this VICE snapshot has no C64 memory in it")
        port, ddr, ram = data[i + 22], data[i + 23], bytes(data[i + 26:i + 26 + 0x10000])
    regs = regs or {"pc": 0, "a": 0, "x": 0, "y": 0, "sp": 0xFF}
    regs |= {"p00": ddr, "p01": port}
    return Dump(ram, regs)


def to_json(regs: dict) -> str:
    return json.dumps({k: f"{v:02X}" if k != "pc" else f"{v:04X}" for k, v in regs.items()})
