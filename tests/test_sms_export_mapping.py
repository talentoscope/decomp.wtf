"""Execute the standalone helper in isolation: source layout is not a live mapping."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from decompnet.rom import sms_mapping


@pytest.fixture
def standalone(tmp_path):
    # Locate the script without importing it: importing would read an exported project's manifest.
    source = Path(sms_mapping.__file__).parent
    shutil.copyfile(source / "export_sms_build.py", tmp_path / "build.py")
    (tmp_path / "tools").mkdir()
    for name in ("z80.py", "sms_identity.py", "sms_mapping.py"):
        shutil.copyfile(source / name, tmp_path / "tools" / name)
    (tmp_path / "build.json").write_text(json.dumps({"sms_profile": {"profile": {
        "version": 1, "mapper": "sega", "origin": "operator"}}}))
    (tmp_path / "layout.json").write_text(json.dumps({"banks": {"0": 0, "1": 0x4000, "2": 0x8000},
        "regions": [], "documented": [], "functions": [
            {"flat": 0x100, "cpu": 0x100, "size": 1, "name": "ResetLoop"},
            {"flat": 0x4100, "cpu": 0x4100, "size": 1, "name": "OwnWindow"},
            {"flat": 0x8100, "cpu": 0x8100, "size": 1, "name": "OtherWindow"}]}))

    def evaluate(program):
        prelude = """import json, runpy
from dataclasses import replace
scope = runpy.run_path('build.py')
from sms_mapping import BusState as S
D = scope['Disassembly']
d = D(bytes([0xc9]) * 0xc000, False)
d.decode()
"""
        proc = subprocess.run([sys.executable, "-I", "-c", prelude + program], cwd=tmp_path,
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    return evaluate


def test_standalone_reference_resolution_does_not_guess_slot_zero_or_other_windows(standalone):
    assert standalone("print(json.dumps([d.resolve(t, 0x100) for t in (0x200, 0x1200, 0x4100, 0x8100)]))") == \
        [0x200, None, None, None]
    assert standalone("print(json.dumps([d.resolve(0x4123, 0x4100), d.resolve(0x8123, 0x4100)]))") == \
        [0x4123, None]


def test_display_geometry_without_an_instruction_is_not_mapping_evidence(standalone):
    assert standalone("print(json.dumps([d.resolve(0x4123, 0x4200), d.resolve(0x200, 0x4200)]))") == [None, None]


def test_explicit_bus_context_wins_and_all_source_kinds_remain_distinct(standalone):
    assert standalone("""s = S.cartridge_only((0, 1, 0))
states = [s, replace(s, control=8), replace(s, bios=True), replace(s, bios=True, cartridge=False),
          replace(s, cartridge=False), S()]
print(json.dumps([d.resolve(0x8123, 0x8100, mapping=s) for s in states]))
""") == [0x123, None, None, None, None, None]


@pytest.mark.parametrize("profile", [None, {}, {"version": 1, "mapper": "unknown", "origin": "legacy"},
    {"version": 1, "mapper": "codemasters", "origin": "operator"},
    {"version": True, "mapper": "sega", "origin": "operator"},
    {"version": 2, "mapper": "sega", "origin": "operator"}])
def test_undeclared_and_unsupported_models_keep_numeric_references(standalone, profile):
    assert standalone(f"scope['B']['sms_profile'] = {{'profile': {profile!r}}}\n"
                      "d = D(bytes([0xc9]) * 0xc000, False)\nd.decode()\n"
                      "print(json.dumps([d.resolve(0x200, 0x100), d.resolve(0x4123, 0x4100)]))") == [None, None]


def test_declared_mapperless_is_flat_without_default_sega_registers(standalone):
    assert standalone("scope['B']['sms_profile']['profile']['mapper'] = 'mapperless'\n"
                      "d = D(bytes([0xc9]) * 0xc000, False)\nd.decode()\n"
                      "print(json.dumps([d.resolve(0x4123, 0x100), d.resolve(0x8123, 0x100),"
                      "d.resolve(0xc123, 0x100)]))") == [0x4123, 0x8123, None]
