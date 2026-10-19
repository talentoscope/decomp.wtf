"""A game imported before it could work: a project that can't be opened, one made read-only before the C toolchain was
installed, one split without mgbdis. Importing the game again sets it right, a project can be removed from the sidebar
even when it can't be opened, and the tools the import needs are installed by the import."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.rom import gb, gb_build, gb_mgbdis

from .test_gb import make_rom as make_gb_rom
from .test_sms import make_rom as make_sms_rom

GBC = make_gb_rom(cgb=0x80, code=bytes.fromhex("cd5001c9") + bytes(0x100) + bytes.fromhex("3e05c9"))
SMS = make_sms_rom(code={0x0000: bytes.fromhex("f3ed56c38000"), 0x0080: bytes.fromhex("cd0001" "18fe"), 0x0100: bytes.fromhex("3e01c9")})


@pytest.fixture
def tools(monkeypatch):
    """Which of the tools the lane looks for are 'installed': flip these to play out the user's sequence of events."""
    state = {"toolchain": False, "mgbdis": False}
    monkeypatch.setattr(gb_build, "available", lambda arch="sm83": state["toolchain"])
    monkeypatch.setattr(gb_mgbdis, "available", lambda: state["mgbdis"])
    return state


def _import(net, rom, name):
    log: list[str] = []
    slug = net.import_rom(rom, name=name, confirmed=True, log=log.append)
    return slug, log


def _statuses(net, slug):
    pid = net.project_row(slug)["id"]
    return {r["status"]: r["n"] for r in net.store.all("SELECT status, COUNT(*) n FROM functions WHERE project_id=? GROUP BY status", pid)}


def _break_like_before(net, slug):
    """What an older version saved when the toolchain was missing: a compiler with no name."""
    proj = net.project_row(slug)
    cfg = json.loads(proj["config"])
    cfg["compiler"]["toolchain"] = ""
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))


def test_a_project_that_cannot_be_opened_is_imported_again(tmp_path, tools):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, _ = _import(net, GBC, "game.gbc")
    _break_like_before(net, slug)
    with pytest.raises(Exception):
        net.overview(slug)  # the state the user was stuck in
    tools["toolchain"] = True  # ...and then the toolchain was installed
    again, log = _import(net, GBC, "game.gbc")
    assert again == slug and any("importing" in line and "again" in line for line in log)
    ov = net.overview(again)
    assert ov["compiler"]["toolchain"] == "gbdk-2020" and ov["functions"] > 0
    assert set(_statuses(net, again)) == {"open"} and net.store.one("SELECT COUNT(*) n FROM projects")["n"] == 1


def test_a_read_only_project_is_imported_again_when_the_toolchain_arrives(tmp_path, tools):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, _ = _import(net, GBC, "game.gbc")
    assert set(_statuses(net, slug)) == {"asm"} and net.overview(slug)["compiler"]["installed"] in (True, False)
    tools["toolchain"] = True
    again, log = _import(net, GBC, "game.gbc")
    assert again == slug and set(_statuses(net, again)) == {"open"}
    assert net.cfg(net.project_row(again))["compiler"]["provenance"] == "explicit"
    assert any("before the C toolchain was installed" in line for line in log)


def test_the_same_goes_for_the_master_system(tmp_path, tools):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, _ = _import(net, SMS, "game.sms")
    assert set(_statuses(net, slug)) == {"asm"}
    tools["toolchain"] = True
    again, _ = _import(net, SMS, "game.sms")
    assert again == slug and set(_statuses(net, again)) == {"open"}


def test_a_project_split_without_mgbdis_is_split_again_once_it_is_installed(tmp_path, tools, monkeypatch):
    tools["toolchain"] = True
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, log = _import(net, GBC, "game.gbc")
    assert net.cfg(net.project_row(slug))["splitter"] == "built-in" and any("WARNING" in line and "approximate" in line for line in log)
    assert "approximate split" in net.project_row(slug)["description"]
    before = net.store.one("SELECT COUNT(*) n FROM functions")["n"]
    # mgbdis arrives (a stand-in for it: two functions, faithful)
    real = [gb.GbFunction(name=f"Call_000_{a:04x}", vram=a, rom=a, code=b"\xc9", n_insns=1, asm=f"{a:04x}  ret", bank=0) for a in (0x150, 0x200)]
    monkeypatch.setattr(gb_mgbdis, "split", lambda path, workdir, log=None, **_: (real, {"faithful": True, "functions": 2}))
    tools["mgbdis"] = True
    again, log = _import(net, GBC, "game.gbc")
    assert again == slug and net.cfg(net.project_row(again))["splitter"] == "mgbdis"
    assert net.store.one("SELECT COUNT(*) n FROM functions")["n"] == 2 != before
    assert "approximate split" not in net.project_row(again)["description"]
    # and now that it is split properly, importing it again changes nothing
    ids = [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")]
    _, log = _import(net, GBC, "game.gbc")
    assert any("already imported" in line for line in log) and [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")] == ids


def test_a_project_with_work_in_it_is_kept_and_repaired_in_place(tmp_path, tools):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, _ = _import(net, GBC, "game.gbc")
    _break_like_before(net, slug)
    pid = net.project_row(slug)["id"]
    fid = net.store.one("SELECT id FROM functions WHERE project_id=? LIMIT 1", pid)["id"]
    agent, _ = net.local_agent("someone")
    with net.store.tx() as db:
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)", (pid, fid, agent.id, "worth keeping", 0))
    ids = [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")]
    tools["toolchain"] = True
    again, log = _import(net, GBC, "game.gbc")
    assert again == slug and any("kept" in line for line in log)
    assert [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")] == ids  # nothing was thrown away
    assert net.store.one("SELECT text FROM notes")["text"] == "worth keeping"
    assert set(_statuses(net, slug)) == {"open"} and net.overview(slug)["compiler"]["toolchain"] == "gbdk-2020"  # opens again


def test_a_game_imported_before_is_not_imported_again_for_nothing(tmp_path, tools):
    tools.update(toolchain=True, mgbdis=False)
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, _ = _import(net, SMS, "game.sms")  # nothing external splits the Z80: not a reason to split it again
    ids = [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")]
    again, log = _import(net, SMS, "game.sms")
    assert again == slug and any("already imported" in line for line in log)
    assert [r["id"] for r in net.store.all("SELECT id FROM functions ORDER BY id")] == ids


# ------------------------------------------------------------------ the import installs what it needs

def _fake_installer(monkeypatch, fail=()):
    installed: set[str] = set()
    calls: list[str] = []
    monkeypatch.setattr(RomNetwork, "auto_install_tools", True)
    monkeypatch.setattr(toolchains, "installed", lambda tid: {"id": tid} if tid in installed else None)

    def install(tid, accept_license=False, log=print):
        calls.append(tid)
        if tid in fail:
            raise RuntimeError("no network")
        installed.add(tid)
    monkeypatch.setattr(toolchains, "install", install)
    return calls


def test_importing_a_game_boy_rom_installs_the_toolchain_and_the_splitter(tmp_path, tools, monkeypatch):
    calls = _fake_installer(monkeypatch)
    _import(RomNetwork(tmp_path / "d.db", tmp_path), GBC, "game.gbc")
    assert calls == ["gbdk-2020", "rgbds", "mgbdis"]


def test_importing_a_master_system_rom_installs_the_toolchain_and_its_assembler(tmp_path, tools, monkeypatch):
    calls = _fake_installer(monkeypatch)
    _import(RomNetwork(tmp_path / "d.db", tmp_path), SMS, "game.sms")
    assert calls == ["gbdk-2020", "wla-dx"]  # (WLA-DX: documented routines and the export)


def test_a_tool_that_cannot_be_installed_does_not_stop_the_import(tmp_path, tools, monkeypatch):
    _fake_installer(monkeypatch, fail={"mgbdis"})
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug, log = _import(net, GBC, "game.gbc")
    assert any("couldn't install" in line and "no network" in line for line in log) and net.overview(slug)["functions"] > 0


def test_tools_already_there_are_left_alone(tmp_path, tools, monkeypatch):
    calls = _fake_installer(monkeypatch)
    monkeypatch.setattr(toolchains, "installed", lambda tid: {"id": tid})
    _import(RomNetwork(tmp_path / "d.db", tmp_path), GBC, "game.gbc")
    assert calls == []


def test_setup_lists_the_tools_that_split_a_game_boy_rom():
    from decompnet.app.engine import TOOLS

    assert "mgbdis" in TOOLS and "rgbds" in TOOLS
    assert {t["id"] for t in toolchains.status()} >= {"mgbdis", "rgbds", "gbdk-2020"}


def test_the_sweep_says_when_it_stopped_at_its_limit(monkeypatch):
    monkeypatch.setattr(gb, "candidates", lambda data, limit=4000: [(0x150 + i, "call") for i in range(4000)])
    log: list[str] = []
    gb.functions(make_gb_rom(), log=log.append)
    assert any("4000" in line and "missing" in line for line in log)


def test_no_message_points_at_a_command_that_installs_nothing():
    root = Path(__file__).resolve().parent.parent / "src" / "decompnet"
    for f in ("rom/gb_build.py", "rom/gb_mgbdis.py", "app/guide/gb.md", "app/guide/sms.md", "app/engine.py"):
        assert "decomp setup" not in (root / f).read_text(encoding="utf-8"), f


# ------------------------------------------------------------------ removing a project that can't be opened

def _app(tmp_path):
    (tmp_path / ".decomp").mkdir(exist_ok=True)
    net = RomNetwork(tmp_path / ".decomp" / "d.db", tmp_path)
    mcp, manager, security = build(net, tmp_path / ".decomp", 6494, "k")
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6494")
    client.cookies.set("decomp_live", "k")
    return net, client


def test_a_project_that_cannot_be_opened_can_be_removed_through_the_api(tmp_path, tools):
    net, client = _app(tmp_path)
    slug, _ = _import(net, GBC, "game.gbc")
    _break_like_before(net, slug)
    listed = {p["slug"]: p for p in client.get("/api/projects").json()}
    assert listed[slug]["broken"]  # the sidebar shows it as one that can't be opened
    root = Path(net.project_row(slug)["root"])
    assert root.exists()
    r = client.post("/api/delete", json={"slug": slug, "files": True}, headers={"X-Decomp": "1"})
    assert r.status_code == 200 and not client.get("/api/projects").json() and not root.exists()
    tools["toolchain"] = True
    again, _ = _import(net, GBC, "game.gbc")  # and the game can be imported again, under its old name
    assert again == slug and net.overview(again)["functions"] > 0


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_sidebar_offers_remove_on_a_project_that_cannot_be_opened():
    js = (Path(__file__).resolve().parent.parent / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index("function renderNav() {")
    fn = js[start:js.index("\n}\n", start) + 3]
    at = js.index("const progress = (p) => {")
    fn = js[at:js.index("\n};\n", at) + 4] + fn  # what the sidebar measures progress with
    script = f"""
let html = "";
const box = {{ set innerHTML(v) {{ html = v; }}, get innerHTML() {{ return html; }}, querySelectorAll: () => [] }};
const $ = () => box, esc = (x) => String(x), setWidths = () => {{}}, markNav = () => {{}}, pctNum = (x) => x.toFixed(0);
const PLAT_SHORT = {{ gbc: "GBC" }};
const S = {{ projects: [
  {{ slug: "azure", title: "AZURE", broken: "unknown toolchain ''", platform: "gbc" }},
  {{ slug: "fine", title: "Fine", platform: "gbc", bytes: 100, matched_bytes: 10 }} ] }};
{fn}
renderNav();
console.log(JSON.stringify(html));
"""
    html = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    broken, fine = html.split('<a class="nav-proj"')[0], html.split('<a class="nav-proj"')[1]
    assert 'data-remove="azure"' in broken and "<a " not in broken  # not a link: there is no page to open
    assert "data-remove" not in fine and 'href="#/p/fine"' in fine
