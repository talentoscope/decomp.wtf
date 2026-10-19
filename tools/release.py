"""Keep package, installer and plugin versions together, with a changelog for each release."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "README.md": r'(The current source version is \*\*)([^*]+)(\*\*)',
    "pyproject.toml": r'(?m)^(version = ")([^"]+)(")',
    "uv.lock": r'(\[\[package\]\]\nname = "decompnet"\nversion = ")([^"]+)(")',
    "src/decompnet/__init__.py": r'(?m)^(__version__ = ")([^"]+)(")',
    "site/public/install.sh": r'(?m)^(VERSION=")([^"]+)(")',
    "site/public/install.ps1": r'(?m)^(\$Version = ")([^"]+)(")',
    "plugins/decomp-wtf/.claude-plugin/plugin.json": r'("version": ")([^"]+)(")',
    ".claude-plugin/marketplace.json": r'("version": ")([^"]+)(")',
}


def versions(root=ROOT):
    result = {}
    for name, pattern in PATTERNS.items():
        found = list(re.finditer(pattern, (root / name).read_text()))
        if len(found) != 1:
            raise ValueError(f"{name}: expected exactly one package version")
        result[name] = found[0].group(2)
    return result


def notes(version, root=ROOT):
    text = (root / "CHANGELOG.md").read_text()
    found = re.search(rf"(?m)^## {re.escape(version)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not found or not found.group(1).strip() or "TODO" in found.group(1):
        raise ValueError(f"CHANGELOG.md needs completed notes for {version}")
    return found.group(1).strip() + "\n"


def check(root=ROOT):
    current = versions(root)
    version = current["pyproject.toml"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or len(set(current.values())) != 1:
        raise ValueError(f"release versions disagree: {current}")
    notes(version, root)
    return version


def bump(version, root=ROOT):
    current = check(root)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        raise ValueError("choose a newer MAJOR.MINOR.PATCH version")
    for name, pattern in PATTERNS.items():
        path = root / name
        path.write_text(re.sub(pattern, lambda m: m.group(1) + version + m.group(3), path.read_text()))
    path = root / "CHANGELOG.md"
    text = path.read_text()
    at = text.index("\n") + 1
    path.write_text(text[:at] + f"\n## {version}\n\n- TODO: describe the changes in this release.\n" + text[at:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "notes", "bump"))
    parser.add_argument("version", nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "bump":
            if not args.version:
                raise ValueError("pass the next version")
            bump(args.version)
            print(f"Updated version fields to {args.version}; complete its CHANGELOG.md entry before release.")
        elif args.action == "notes":
            print(notes(check()), end="")
        else:
            print(check())
    except (ValueError, OSError) as error:
        parser.exit(1, f"release: {error}\n")


if __name__ == "__main__":
    main()
