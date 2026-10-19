"""Sandboxed compilation of untrusted agent-submitted C.

Agents never get to run their own compiler for anything that counts: every score and
every match is produced here, on the coordinator, with the project's pinned toolchain.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import wsl

DUMP_PASSES = ("rtl", "jump", "cse", "addressof", "gcse", "loop", "cse2", "bp", "flow",
               "combine", "regmove", "sched", "lreg", "greg", "flow2", "sched2", "jump2", "mach", "dbr", "stack")
# Numbered dump names emitted by the pinned EE-GCC 2.96 pipeline.
EE_DUMPS = ("00.rtl", "01.sibling", "02.jump", "03.cse", "04.addressof", "05.ssa", "06.ccp",
            "07.dce", "08.gcse", "09.loop", "10.cse2", "11.cfg", "12.bp", "13.life", "14.combine",
            "15.ce", "16.regmove", "17.sched", "18.stack", "19.lreg", "20.greg", "21.flow2",
            "22.ce2", "23.rnreg", "24.bbro", "25.sched2", "26.stack", "27.jump2", "28.mach", "29.dbr")
DUMP_NAMES = (*DUMP_PASSES, *EE_DUMPS)
MAX_DUMP_BYTES = 4 << 20
MAX_SOURCE_BYTES = 64 * 1024
# Per-compile resource caps (address space, CPU seconds, output file size) and a global
# concurrency cap, so a hostile submission can't take the host down with it.
LIMITS = ["--as=2147483648", "--cpu=30", "--fsize=67108864"]
_SLOTS = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) - 1))

_COMMENTS_AND_STRINGS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
# SDCC's own spelling (Game Boy, Master System, NES): it leaves out the function's return, so its body alone
# reproduces a piece of a hand-written routine that no C function is
NAKED = re.compile(r"\b__naked\b")
_FORBIDDEN = [
    (re.compile(r"\b(__)?asm(__)?\b"), "inline assembly is not allowed: matches must come from C"),
    (re.compile(r"\b(GLOBAL_ASM|INCLUDE_ASM|INCLUDE_RODATA|NON_MATCHING)\b"), "asm-include macros are not allowed"),
    (re.compile(r"\.incbin\b"), ".incbin is not allowed"),
    (re.compile(r"__attribute__\s*\(\(\s*(section|naked)\b"), "section/naked attributes are not allowed"),
    (NAKED, "__naked is not allowed: it leaves out the function's return, and a match must be a whole function "
            "from C"),
    (re.compile(r"(#\s*pragma|\b_Pragma)\b"), "pragmas are not allowed (ask the project admin to whitelist one)"),
    (re.compile(r"#\s*include\s*<"), "system includes are not allowed; everything you need is in context.h"),
]
_INCLUDE = re.compile(r'#\s*include\s*"([^"]+)"')

# A register pin is a *declaration*, not a statement:
#     register u32 t __asm__("$4");
# It only constrains which register a variable lives in -- every instruction still comes from the
# compiler -- and it is the documented technique for register-allocation hard tails (BFM-decomp
# matching cookbook, section 10: "Closing the regalloc/scheduling hard tail by hand"). The pattern
# accepts no parentheses, braces or semicolons between `register` and the asm clause, so it cannot
# hide another statement, and the register name itself is restricted to a plain name or "$N".
# __asm__ *statements* (e.g. `__asm__ __volatile__("" : : "r"(x));`) remain forbidden.
_REGISTER_PIN = re.compile(
    r'\bregister\b[^;{}()]*?\b(?:__)?asm(?:__)?\s*\(\s*"(\$?[A-Za-z0-9_]+)"\s*\)\s*;'
)


class SourceRejected(ValueError):
    pass


def check_includes(text: str) -> None:
    """No include may reach outside the compile's own folder: a header or source that names a file elsewhere on the
    computer would put it into the build and its error messages (a room shares those with its other members)."""
    for inc in _INCLUDE.findall(text):
        if inc.startswith("/") or re.match(r"^[A-Za-z]:", inc) or ".." in re.split(r"[/\\]", inc):
            raise SourceRejected(f"include path not allowed: {inc}")


def uses_naked(source: str) -> bool:
    """`__naked` in the code itself (not in a comment or a string)."""
    return NAKED.search(_COMMENTS_AND_STRINGS.sub(" ", source or "")) is not None


def check_source(source: str, *, check_size: bool = True) -> None:
    if check_size and len(source.encode()) > MAX_SOURCE_BYTES:
        raise SourceRejected(f"source too large (max {MAX_SOURCE_BYTES} bytes)")
    check_includes(source)
    # Remove register pins before masking comments/strings (the pin's own string is the register
    # name, which the masking below would otherwise hide from the pin pattern).
    pinned = _REGISTER_PIN.sub("register ", source)
    code = _COMMENTS_AND_STRINGS.sub(" ", pinned)
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            raise SourceRejected(why)


@dataclass
class CompilerConfig:
    cmd: list[str]
    timeout: float = 15.0
    sandbox: str = "auto"  # auto | bwrap | docker | none
    image: str | None = None  # for sandbox = "docker"
    ro_paths: tuple[str, ...] = ()
    include_dirs: tuple[str, ...] = ()  # absolute paths of extra (static) header dirs
    runtime32: str | None = None  # 32-bit x86 C runtime for old i386 compiler binaries (see toolchains)
    wsl: bool = False  # a Linux toolchain on Windows: runs inside WSL 2, `cmd` holds WSL paths (see wsl.py)
    psx_gte: bool = False  # the compiler supplies the immutable GTE intrinsics header


@dataclass
class CompileResult:
    ok: bool
    obj: bytes | None
    log: str
    artifacts: dict[str, str] = field(default_factory=dict)


def _sandbox_mode(cfg: CompilerConfig) -> str:
    if cfg.wsl:
        return "wsl"
    if cfg.sandbox != "auto":
        return cfg.sandbox
    return "bwrap" if shutil.which("bwrap") else "none"


def _root(cfg: CompilerConfig, work: Path) -> str:
    """How the compiler sees the work folder (in WSL it works on a copy, see wsl.run)."""
    mode = _sandbox_mode(cfg)
    return "/work" if mode in ("bwrap", "docker") else wsl.stage_dir() if mode == "wsl" else str(work)


def _wrap(cfg: CompilerConfig, work: Path, argv: list[str]) -> list[str]:
    mode = _sandbox_mode(cfg)
    if mode in ("none", "bwrap") and shutil.which("prlimit"):
        argv = ["prlimit", *LIMITS, "--", *argv]
    if mode == "none":
        return argv
    if mode == "docker":
        if not cfg.image:
            raise RuntimeError("sandbox = 'docker' needs compiler.image")
        return ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "64",
                "-v", f"{work}:/work", "-w", "/work", cfg.image, *argv]
    if mode == "bwrap":
        # /tmp first, so read-only mounts of paths that live under /tmp are not hidden by it.
        cmd = ["bwrap", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
               "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev"]
        if cfg.runtime32:
            # i386 binaries hardcode /lib/ld-linux.so.2; give them a private /lib with the pinned loader,
            # and point the loader at the pinned 32-bit libraries. 64-bit tools ignore those libraries.
            cmd += ["--dir", "/lib", "--ro-bind", f"{cfg.runtime32}/ld-linux.so.2", "/lib/ld-linux.so.2",
                    "--ro-bind", cfg.runtime32, "/opt/rt32", "--setenv", "LD_LIBRARY_PATH", "/opt/rt32"]
        else:
            cmd += ["--symlink", "usr/lib", "/lib"]
        for p in cfg.ro_paths:
            cmd += ["--ro-bind", p, p]
        cmd += ["--bind", str(work), "/work", "--chdir", "/work", "--unshare-all", "--die-with-parent", "--new-session"]
        return [*cmd, "--", *argv]
    raise RuntimeError(f"unknown sandbox mode {mode!r}")


def _exec(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float, cwd: Path | None = None, **stage):
    """`stage`: in WSL, run on a copy in WSL's own file system (wsl.run's outputs, inputs and stage)."""
    if _sandbox_mode(cfg) == "wsl":
        return wsl.run(argv, cwd or work, timeout, **stage)
    # in the work folder, never in the app's own: KMC GCC's assembler (binutils 2.6) can't read its input when the
    # current directory is / ("Bad file descriptor"), which it is for decomp.wtf started by a service or in Docker
    return subprocess.run(_wrap(cfg, work, argv), cwd=cwd or work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def run_in(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float = 300, *,
           inputs: list[str] | None = None, outputs: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a toolchain command inside `work` (the sandbox's /work), e.g. to build a whole exported project.
    Paths in `argv` must be relative to `work`. In WSL, naming the files it reads (`inputs`) and writes (`outputs`)
    lets old 32-bit compilers work on a copy in WSL's own file system, which they need (see wsl.run)."""
    return _exec(cfg, Path(work), argv, timeout, cwd=Path(work),
                 **({} if outputs is None else {"inputs": inputs, "outputs": outputs}))


def compile_source(cfg: CompilerConfig, context_header: str, source: str, trusted: bool = False, *, diagnostics: bool = False) -> CompileResult:
    if not trusted:
        try:
            check_source(source)
            if cfg.psx_gte:
                from .psx_gte import check_overrides

                check_source(re.sub(r"\\\r?\n", "", context_header), check_size=False)
                check_source(re.sub(r"\\\r?\n", "", source))
                check_overrides(context_header)
                check_overrides(source)
        except SourceRejected as e:
            return CompileResult(False, None, f"rejected: {e}")

    with tempfile.TemporaryDirectory(prefix="decompnet-") as tmp:
        work = Path(tmp)
        inc = work / "include"
        inc.mkdir()
        for d in cfg.include_dirs:
            shutil.copytree(d, inc, dirs_exist_ok=True)
        if cfg.psx_gte:
            from .psx_gte import header_path

            shutil.copyfile(header_path(), inc / "psx_gte.h")
        # LF on every platform: to the old GCCs a backslash before CRLF doesn't continue a line ("stray '\' in program")
        (inc / "context.h").write_text(context_header, encoding="utf-8", newline="\n")
        prefix = '#include "psx_gte.h"\n' if cfg.psx_gte else ""
        (work / "func.c").write_text(prefix + '#include "context.h"\n#line 1 "func.c"\n' + source + "\n", encoding="utf-8",
                                     newline="\n")

        root = _root(cfg, work)
        subst = {"{src}": f"{root}/func.c", "{obj}": f"{root}/func.o", "{include}": f"{root}/include"}
        argv = [subst.get(a, a) for a in cfg.cmd]
        argv = [a.replace("{include}", subst["{include}"]) for a in argv]
        if diagnostics:
            argv.append("-da")
        outputs = ["func.o"] + ([f"func.c.{suffix}" for suffix in DUMP_NAMES] if diagnostics else [])
        try:
            with _SLOTS:
                proc = _exec(cfg, work, argv, cfg.timeout,
                             **({"outputs": outputs, "stage": root} if _sandbox_mode(cfg) == "wsl" else {}))
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"compiler timed out after {cfg.timeout}s")
        except RuntimeError as e:
            return CompileResult(False, None, str(e))
        log = (proc.stdout + proc.stderr).replace(root + "/", "")
        obj = work / "func.o"
        if proc.returncode != 0 or not obj.exists():
            return CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}")
        artifacts = {}
        if diagnostics:
            used = 0
            for suffix in DUMP_NAMES:
                path = work / ("func.c." + suffix)
                if path.is_file():
                    used += path.stat().st_size
                    if used > MAX_DUMP_BYTES:
                        return CompileResult(False, None, "compiler dump capture exceeds 4 MiB; reduce the source")
                    artifacts[suffix] = path.read_text(encoding="utf-8", errors="replace").replace(root + "/", "")
        return CompileResult(True, obj.read_bytes(), log[-2000:], artifacts)
