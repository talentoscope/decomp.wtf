"""Standalone PS1 compile driver: GCC assembly -> maspsx -> pinned GNU assembler.

This file runs inside the compile sandbox with the system Python and uses only its standard library.
Arguments are passed directly to programs; source and flags are never interpreted as shell commands.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ASPSX_VERSIONS = ("1.05", "2.21", "2.34", "2.56", "2.67", "2.77", "2.79", "2.81", "2.86")


def validate_version(version: str) -> str:
    if version not in ASPSX_VERSIONS:
        raise ValueError("unsupported ASPSX version: " + str(version))
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maspsx", required=True)
    parser.add_argument("--assembler", required=True)
    parser.add_argument("--aspsx-version", required=True, choices=ASPSX_VERSIONS)
    parser.add_argument("compiler", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cc = args.compiler
    if cc and cc[0] == "--":
        cc = cc[1:]
    if "-c" not in cc or "-o" not in cc or cc.index("-o") + 1 >= len(cc):
        parser.error("expected a GCC compile command with -c and -o")
    out_at = cc.index("-o") + 1
    output = cc[out_at]
    assembly = output + ".s"
    cc[cc.index("-c")] = "-S"
    cc[out_at] = assembly
    result = subprocess.run(cc)
    if result.returncode:
        return result.returncode
    # Both -G8 and -G 8 are accepted by GCC; the last setting wins.
    threshold = "8"
    for i, flag in enumerate(cc):
        if flag == "-G" and i + 1 < len(cc):
            threshold = cc[i + 1]
        elif flag.startswith("-G") and len(flag) > 2:
            threshold = flag[2:]
    try:
        int(threshold)
    except ValueError:
        parser.error("-G requires an integer threshold")
    cmd = [sys.executable, args.maspsx, "--aspsx-version", args.aspsx_version, "--expand-div",
           "--run-assembler", "--gnu-as-path", args.assembler, "--force-stdin",
           "-EL", "-march=r3000", "-mtune=r3000", "-no-pad-sections", "-O1", f"-G{threshold}", "-o", output]
    try:
        return subprocess.run(cmd, input=Path(assembly).read_text(encoding="utf-8"), text=True).returncode
    finally:
        Path(assembly).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
