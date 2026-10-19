"""Briefs that don't bury the function (notes cut short, a long listing in parts, a function many agents gave back
said so and handed out last), tools that take either spelling of a note, and helpers that clean up after a crash,
wait out a race for a ★ function, and stop with the autopilot."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from decompnet.app import cli, tools
from decompnet.app.agents import Manager, Session
from decompnet.app.permute import State
from decompnet.app.engine import RomNetwork
from decompnet.network import GIVEN_UP, NOTE_SHOWN


def _net(tmp_path, n_insns=10):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"kind": "rom", "priority": ["star"],
                                               "compiler": {"toolchain": "gbdk-2020", "flags": []}})))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,1,'',NULL,'',0,'')")
        for k, (name, insns) in enumerate((("f", n_insns), ("easy", 1), ("star", 5))):
            db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, vram, rom_offset) "
                       "VALUES(1,?,?,?,?,'[]',?,?,?)", (name, insns * 4, insns, b"\0" * insns * 4,
                                                       "\n".join(f"    /* {i:X} */ nop" for i in range(insns)),
                                                       0x80001000 + k * 0x10000, 0x1000 + k * 0x10000))
    return net


def test_a_brief_cuts_long_notes_and_says_when_many_agents_gave_the_function_back(tmp_path):
    net = _net(tmp_path)
    agent, _ = net.local_agent("claude-1")
    net.add_note(agent, "game", "x" * 3000, "f")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET abandons=? WHERE name='f'", (GIVEN_UP,))
    brief = net.get_function("f", "game", agent)
    assert "x" * NOTE_SHOWN in brief and "x" * (NOTE_SHOWN + 1) not in brief and "1500 more characters" in brief
    assert f"gave this function back {GIVEN_UP} times" in brief
    other, _ = net.local_agent("codex-1")
    assert net.claim_row(other, "game", strategy="easiest")["name"] != "f"  # handed out after the rest


def test_a_long_listing_comes_in_parts(tmp_path):
    net = _net(tmp_path, n_insns=900)
    brief = net.get_function("f", "game")
    assert "start_line=400" in brief and "/* 18F */" in brief and "/* 190 */" not in brief
    part = net.get_function("f", "game", start_line=400)
    assert "lines 400-799 of 900" in part and "/* 190 */" in part and "start_line=800" in part
    assert "of 900" in tools.call(net, net.local_agent("api-1")[0], "game", "get_function",
                                  {"function": "f", "start_line": 800})


def test_notes_are_taken_whichever_way_the_model_spells_them(tmp_path):
    net = _net(tmp_path)
    agent, _ = net.local_agent("api-1")
    assert not tools.call(net, agent, "game", "add_note", {"notes": "uses the player struct", "function": "f"}).startswith("error")
    net.claim_row(agent, "game", "easy")
    out = tools.call(net, agent, "game", "release_function", {"function": "easy", "text": "data, not code"})
    assert out.startswith("Released") and net.store.one("SELECT 1 FROM notes WHERE text='data, not code'")


def test_a_starred_only_helper_that_loses_the_race_for_a_star_waits(tmp_path, monkeypatch):
    net = _net(tmp_path)
    m = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    left = iter([(1, 1)] + [(1, 0)] * 100)  # free at the check, taken by another helper before this one claims
    monkeypatch.setattr(m, "_starred_left", lambda slug, agent_id, since=0.0: next(left))
    monkeypatch.setattr(m, "_run_cli", lambda s, token, workdir: True)  # it ended without compiling
    s = Session("codex-1", "codex", "Codex #1", "game", "codex-1", {"only_starred": True})
    threading.Thread(target=m._run, args=(s,), daemon=True).start()
    for _ in range(100):
        if s.state == "waiting":
            break
        time.sleep(0.02)
    assert s.state == "waiting" and not s.error and any("taken by others" in f["text"] for f in s.feed)
    s.stop.set()


def test_stopping_the_autopilot_stops_the_permuter(tmp_path):
    from starlette.testclient import TestClient

    from decompnet.app.server import build

    from .test_tiered_helpers import _net as host_net

    net, home = host_net(tmp_path, {"kind": "rom", "compiler": {"toolchain": "gbdk-2020", "flags": []}})
    mcp, manager, security = build(net, home / ".decomp", 6496, "k")
    net.overview = lambda slug: {}  # (what the page shows next isn't the point here)
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6496")
    c.cookies.set("decomp_live", "k")
    st = net.permuter.jobs["demo"] = State()  # as if it were rewriting the project's near misses
    assert c.post("/api/autopilot", json={"slug": "demo", "action": "stop"}, headers={"X-Decomp": "1"}).status_code == 200
    assert st.stop.is_set()


def test_decomp_stop_waits_until_the_app_is_gone(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    (home / ".decomp" / "server.json").write_text(json.dumps({"port": 1, "secret": "s", "pid": 1}))
    monkeypatch.setenv("DECOMP_HOME", str(home))
    alive = iter([True, True, True, False])
    monkeypatch.setattr(cli, "_alive", lambda port: next(alive))
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda req, timeout=5: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.stop(None) == 0 and "stopped" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_what_a_crash_left_running_is_stopped_and_its_claims_released(tmp_path):
    net = _net(tmp_path)
    m = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    stray = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        m._running_procs(add=(stray.pid, "codex-1", os.path.basename(sys.executable)))
        m._running_procs(add=(other.pid, "claude-1", "claude"))  # its pid now runs something else: left alone
        helper, _ = net.local_agent("codex-1")
        mine = net.authenticate(net.register_agent("my-agent", origin="chat"))
        net.claim_row(helper, "game", "f")
        net.claim_row(mine, "game", "easy")
        assert m.stop_strays() == ["codex-1"]
        assert stray.wait(10) is not None and other.poll() is None
        held = {r["name"] for r in net.store.all("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id")}
        assert held == {"my-agent"}  # an agent of the user's own keeps its claim
        assert m._running_procs() == {}
    finally:
        for p in (stray, other):
            if p.poll() is None:
                p.kill()
