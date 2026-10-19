"""Trusted PS1 GTE intrinsics and context types; submissions cannot replace the intrinsics."""

from __future__ import annotations

from pathlib import Path
import re

TYPES = """
/* PS1 context types: 32-bit integers, packed short vector components. */
typedef struct { short vx, vy, vz, pad; } SVECTOR;
typedef struct { int vx, vy, vz, pad; } VECTOR;
typedef struct { short vx, vy; } DVECTOR;
typedef struct { unsigned char r, g, b, cd; } CVECTOR;
typedef struct { short x, y, w, h; } RECT;
typedef struct { short m[3][3]; int t[3]; } MATRIX;
"""

_OVERRIDE = re.compile(r"^\s*#\s*(?:define|undef)\s+(?:gte_\w*|__decomp_gte\w*|DECOMP_PSX_GTE_H)\b", re.M)
COMMANDS = {1, 6, 12, 16, 17, 18, 19, 20, 22, 27, 28, 30, 32, 40, 41, 42, 45, 46, 48, 61, 62, 63}


def header_path() -> Path:
    return Path(__file__).with_name("psx_gte.h").resolve()


def check_overrides(text: str) -> None:
    from .compiler import SourceRejected, _COMMENTS_AND_STRINGS

    text = _COMMENTS_AND_STRINGS.sub(" ", re.sub(r"\\\r?\n", "", text))
    if _OVERRIDE.search(text):
        raise SourceRejected("trusted GTE macros cannot be redefined or undefined")


def enabled(cfg: dict, unit: str | None = None) -> bool:
    from . import toolchains
    from .project import compiler_settings

    profile = compiler_settings(cfg, unit)
    return bool(profile.get("gte", True) and profile.get("toolchain") and
                toolchains.get(profile["toolchain"]).platform == "PS1")


def is_gte(word: int) -> bool:
    return word >> 26 in (0x12, 0x32, 0x3A)


def count(code: bytes) -> int:
    return sum(is_gte(int.from_bytes(code[i:i + 4], "little")) for i in range(0, len(code) - 3, 4))
