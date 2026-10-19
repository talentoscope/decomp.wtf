"""A Master System project exported as WLA-DX assembly: the project's names and notes in a disassembly made from the
user's copy at build time, routines documented in assembly in place of their bytes, and a build that remakes the game
byte for byte. The tree holds the project's work, never the game's bytes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import sms_asm

from .test_sms import make_rom

pytestmark = pytest.mark.skipif(not sms_asm.available(), reason="needs WLA-DX")

CODE = {
    0x0000: bytes.fromhex("c30001"),
    0x0100: bytes.fromhex("cd0002" "3e05" "32ffff" "cd0080" "18fe"),  # call ShowMenu · map bank 5 · call $8000 · loop
    0x0200: bytes.fromhex("3aa2c0" "a7" "c8" "3e81" "d3bf" "c9"),  # ld a,($c0a2) · and a · ret z · ld a,$81 · out ($bf),a
    0x14000: bytes.fromhex("3e07c9"),                              # bank 5, at $8000
}
SHOW = """; Turns the screen on when a menu is open (the flag the menu
; code sets). In: nothing. Out: A trashed.
ShowMenu:
    ld a, (wMenuOpen)
    and a
    ret z
    ld a, $81
    out (PortVDPControl), a
    ret
"""


@pytest.fixture(params=["sms", "gg"])
def exported(tmp_path, request):
    net = RomNetwork(tmp_path / "d.db", tmp_path / "ws")
    rom = tmp_path / f"game.{request.param}"
    rom.write_bytes(make_rom(size=0x20000, region=6 if request.param == "gg" else 4, code=CODE))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    net.sms.configure(slug, "sega", "annotated-original", "The authored fixture specifies Sega cartridge mapping.")
    pid = net.project_row(slug)["id"]
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND rom_offset=?", pid, 0x200)
    with net.store.tx() as db:  # documented in assembly, its name the project's
        db.execute("UPDATE functions SET name='ShowMenu', status='documented', matched_source=? WHERE id=?", (SHOW, fn["id"]))
        db.execute("UPDATE symbols SET name='ShowMenu' WHERE project_id=? AND name=?", (pid, fn["name"]))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)", (pid, "wMenuOpen", 0xC0A2, 0, "data"))
    res = net.export_rom(slug, log=lambda *_: None)
    return net, slug, res, Path(res["path"])


def test_the_export_rebuilds_the_game_with_its_documented_routine(exported):
    net, slug, res, out = exported
    assert res["identical"], res["log"]
    assert (res["documented"], res["left_out"]) == (1, [])
    platform = net.cfg(net.project_row(slug))["platform"]
    assert (out / "build" / f"game.{platform}").read_bytes() == (out / f"baserom.{platform}").read_bytes()
    assert net.overview(slug)["exportable"] and net.cfg(net.project_row(slug))["export"]["documented"] == 1
    bank0 = (out / "build" / "disassembly" / "bank_000.asm").read_text()
    assert "call ShowMenu" in bank0 and "; documented in assembly: documented/ShowMenu.asm" in bank0
    assert "    out (PortVDPControl), a" in bank0 and "ld a, (wMenuOpen)" in bank0
    assert ".BANK 5 SLOT 2" in (out / "build" / "disassembly" / "bank_005.asm").read_text()
    assert ".DEFINE wMenuOpen $C0A2" in (out / "documented" / "names.inc").read_text()
    assert {"layout.json", "comments.txt", "build.py", "build.json", "README.md", ".gitignore"} <= {p.name for p in out.iterdir()}
    assert (out / "tools" / "z80.py").exists() and (out / "include" / "hardware.inc").exists()
    from decompnet.rom import sms_mapping
    assert (out / "tools" / "sms_mapping.py").read_bytes() == Path(sms_mapping.__file__).read_bytes()
    plain = subprocess.run([sys.executable, "build.py", "--no-documented"], cwd=out, capture_output=True, text=True)
    assert plain.returncode == 0 and "identical" in plain.stdout, plain.stdout + plain.stderr


def test_the_tree_keeps_the_game_out_of_git(exported):
    net, slug, res, out = exported
    base = f"baserom.{net.cfg(net.project_row(slug))['platform']}"
    ignored = (out / ".gitignore").read_text().split()
    assert "build/" in ignored and base in ignored
    tracked = [p for p in out.rglob("*") if p.is_file() and "build" not in p.relative_to(out).parts
               and p.name != base and "__pycache__" not in p.parts]
    rom = (out / base).read_bytes()
    assert all(rom[0x100:0x10f] not in p.read_bytes() for p in tracked)  # the game's code is in none of them
    layout = json.loads((out / "layout.json").read_text())
    assert any(f["name"] == "ShowMenu" and f["cpu"] == 0x200 for f in layout["functions"])


def test_the_build_refuses_another_game(exported):
    net, slug, res, out = exported
    base = f"baserom.{net.cfg(net.project_row(slug))['platform']}"
    (out / base).write_bytes(make_rom(size=0x20000, code={**CODE, 0x0200: bytes.fromhex("3e01c9")}))
    proc = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True)
    assert proc.returncode != 0 and "isn't the version" in proc.stderr


def test_export_refuses_a_changed_comparison_target_before_replacing_output(exported):
    net, slug, _res, out = exported
    proj = net.project_row(slug)
    cfg = net.cfg(proj)
    manifest = (out / "build.json").read_bytes()
    image = Path(proj["root"]) / cfg["rom"]["file"]
    changed = bytearray(image.read_bytes())
    changed[0x200] ^= 1
    image.write_bytes(changed)
    with pytest.raises(ValueError, match="frozen normalized identity"):
        net.export_rom(slug, log=lambda *_: None)
    assert (out / "build.json").read_bytes() == manifest
    assert net.cfg(net.project_row(slug))["rom"] == cfg["rom"]


def test_standalone_build_enforces_normalized_identity(exported):
    _net, _slug, _res, out = exported
    path = out / "build.json"
    manifest = json.loads(path.read_text())
    manifest["identity"]["normalized"]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    proc = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True)
    assert proc.returncode != 0 and "frozen normalized identity" in proc.stderr


@pytest.mark.parametrize("size", [0x2000, 0x4000, 0xC000])
@pytest.mark.parametrize("region, platform", [(4, "sms"), (6, "gg")])
def test_standalone_build_normalizes_small_copier_inputs(tmp_path, size, region, platform):
    net = RomNetwork(tmp_path / "state.db", tmp_path / "projects")
    rom = make_rom(size=size, region=region, code={0: bytes.fromhex("18fe")})
    slug = net.import_rom(rom, confirmed=True, name=f"input.{platform}", log=lambda *_: None)
    res = net.export_rom(slug, log=lambda *_: None)
    assert res["identical"], res["log"]
    assert not res["sms_profile"]["supported"]  # byte reconstruction does not establish the mapper
    out = Path(res["path"])
    (out / f"baserom.{platform}").write_bytes(bytes(512) + rom)
    proc = subprocess.run([sys.executable, "build.py"], cwd=out, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (out / "build" / f"game.{platform}").read_bytes() == rom


def test_export_records_experimental_intent_without_retargeting_original_bytes(exported):
    net, slug, _res, out = exported
    before = net.cfg(net.project_row(slug))["rom"]
    net.sms.configure(slug, "sega", "experimental", "Study modifications separately from original reconstruction.")
    result = net.export_rom(slug, log=lambda *_: None)
    assert result["identical"] and not result["exact_original_intent"]
    manifest = json.loads((out / "build.json").read_text())
    assert manifest["sms_profile"]["profile"]["variant"] == "experimental"
    assert net.cfg(net.project_row(slug))["rom"] == before


def test_legacy_export_keeps_byte_verification_separate_from_mapping_declaration(exported):
    net, slug, _res, _out = exported
    net._update_cfg(slug, lambda cfg: cfg.pop("sms_profile"))
    result = net.export_rom(slug, log=lambda *_: None)
    assert result["identical"] and result["documented"] == 1
    assert result["sms_profile"]["status"] == "legacy" and not result["sms_profile"]["supported"]
