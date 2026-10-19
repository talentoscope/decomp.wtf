"""Run the synthetic SMS/GG assembly checks with pinned tools and no skipped tests."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from decompnet import toolchains

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/test_sms_asm.py",
    "tests/test_sms_documented.py",
    "tests/test_export_sms.py",
)


def require_tools() -> str:
    """Check the install manifest against the registry, not a fallback assembler on PATH."""
    manifest = toolchains.installed("wla-dx")
    spec = toolchains.get("wla-dx")
    if manifest is None:
        raise ValueError("install the pinned tools: decompnet toolchain install wla-dx")
    expected = {(p["url"], p["sha256"]) for p in spec.parts_for()}
    installed = {(p["url"], p["sha256"]) for p in manifest.get("parts", [])}
    if not expected or installed != expected or manifest.get("source") != spec.source:
        raise ValueError("WLA-DX install differs from the registry pin; reinstall with --force")
    folder = toolchains.path("wla-dx")
    for name in ("wla-z80", "wlalink"):
        exe = folder / (name + (".exe" if sys.platform == "win32" else ""))
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise ValueError(f"pinned WLA-DX executable is missing or not executable: {name}")
    return spec.source


def verify_report(report: Path) -> int:
    """Require successful execution in each module, including the GG export cases."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError("SMS gate did not produce a valid JUnit report") from exc
    cases = list(root.iter("testcase"))
    if not cases or any(case.find(tag) is not None for case in cases for tag in ("skipped", "error", "failure")):
        raise ValueError("SMS gate requires executed, passing tests with no skips")
    modules = {case.get("classname") for case in cases}
    if not all(test.removesuffix(".py").replace("/", ".") in modules for test in TESTS):
        raise ValueError("SMS gate report is missing a required test module")
    if not any(case.get("classname") == "tests.test_export_sms" and "[gg]" in case.get("name", "")
               for case in cases):
        raise ValueError("SMS gate report is missing Game Gear export checks")
    return len(cases)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    # Keep artifacts ignored by default, and remove stale reports before starting the process.
    report = (args.report or ROOT / "build" / "sms-tool-tests.xml").resolve()
    try:
        print(f"SMS gate toolchain: {require_tools()}", flush=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.unlink(missing_ok=True)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               f"--junitxml={report}", *TESTS], cwd=ROOT)
        if proc.returncode:
            return proc.returncode
        count = verify_report(report)
    except ValueError as exc:
        print(f"SMS gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"SMS gate: {count} passed, zero skipped (SMS and GG)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
