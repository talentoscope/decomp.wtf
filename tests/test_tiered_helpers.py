"""Tiered Claude Code helpers: the model (and effort) is picked per function, right before each session."""

import json

from starlette.testclient import TestClient

from decompnet.app import agents
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build

HAIKU, SONNET, OPUS = agents.HAIKU, agents.SONNET, agents.OPUS


def _state(**kw):
    return {"best_score": None, "n_insns": 100, "attempts": 0} | kw


def test_every_tier_of_the_policy_table():
    # the worst score for 100 instructions is 10000
    assert agents.tier_for(_state()) == (SONNET, "medium", "fresh")  # nothing compiled yet
    assert agents.tier_for(_state(best_score=500, attempts=3)) == (HAIKU, "low", "near_miss")  # 5%
    assert agents.tier_for(_state(best_score=3000, attempts=3)) == (HAIKU, "low", "mechanical")  # 30%
    assert agents.tier_for(_state(best_score=8000, attempts=3)) == (SONNET, "medium", "structural")  # 80%
    # still far off after the stall threshold: Opus
    assert agents.tier_for(_state(best_score=8000, attempts=agents.STALL_ATTEMPTS)) == (OPUS, "high", "escalated")
    # ...while a near miss stays with Haiku however many attempts it took
    assert agents.tier_for(_state(best_score=500, attempts=agents.STALL_ATTEMPTS + 5)) == (HAIKU, "low", "near_miss")
    assert agents.tier_for(_state(best_score=0, n_insns=0)) == (HAIKU, "low", "near_miss")  # no division by zero
    # every tier in the table names a Claude Code alias and an effort its --effort takes
    for model, effort in agents.TIERS.values():
        assert model in (HAIKU, SONNET, OPUS) and effort in agents.EFFORTS


def _net(tmp_path, config=None):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1, 'demo', 'demo', ?, ?)",
                   (str(tmp_path), json.dumps(config or {"kind": "rom"})))
    return net, home


def _add(net, name, n_insns, best_score=None, attempts=0):
    with net.store.tx() as db:
        db.execute("INSERT INTO functions(project_id, name, size, n_insns, status, best_score, attempts, abandons) "
                   "VALUES(1, ?, ?, ?, 'open', ?, ?, 0)", (name, n_insns * 4, n_insns, best_score, attempts))
    return net.store.one("SELECT * FROM functions WHERE name=?", name)


def test_the_model_is_picked_for_the_function_the_helper_claims_next(tmp_path):
    net, home = _net(tmp_path)
    m = agents.Manager(net, home, "http://127.0.0.1:6464/mcp")
    s = agents.Session("claude-1", "claude", "Claude Code #1", "demo", "claude-1", {"model": "tiered", "per_session": 1})
    assert m.tier(s) == (SONNET, "medium", "fresh", "")  # nothing to decompile yet: the fresh tier, no function named
    _add(net, "big_far", 200, best_score=16000, attempts=2)  # 80%: structural
    _add(net, "small_close", 20, best_score=100, attempts=3)  # 5%: a near miss, and the easiest (fewest instructions)
    assert m.tier(s) == (HAIKU, "low", "near_miss", "small_close")
    # a ★ Next in line pin comes before the order
    net.set_priority("demo", "big_far")
    assert m.tier(s) == (SONNET, "medium", "structural", "big_far")
    net.set_priority("demo", "big_far", False)
    # a claim this agent still holds comes first, whatever the order says
    agent = net.authenticate(net.register_agent("claude-1"))
    s.agent_id = agent.id
    big = net.store.one("SELECT id FROM functions WHERE name='big_far'")["id"]
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (big, agent.id, 0, 10 ** 12))
    assert m.tier(s) == (SONNET, "medium", "structural", "big_far")
    # the Agents tab sees the chosen model and tier, never the word "tiered" as a model
    s.routed_model, s.routed_effort, s.tier, s.tier_function = m.tier(s)
    snap = s.snapshot()
    assert snap["model"] == SONNET and snap["effort"] == "medium" and snap["tier"] == "structural" and snap["tiered"]
    assert snap["tier_function"] == "big_far"


def test_a_git_project_has_no_pins_and_uses_its_own_order(tmp_path):
    net, _ = _net(tmp_path, {"kind": "fork"})
    _add(net, "a", 90)
    _add(net, "b", 10)
    net.fork_slug = lambda slug: slug  # as a git project
    assert agents.next_function(net, "demo")["name"] == "b"  # fewest instructions first


def test_the_command_line_carries_the_picked_model_and_effort(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    m = agents.Manager.__new__(agents.Manager)
    m.mcp_url = "http://127.0.0.1:6464/mcp"
    m.prompt = lambda s: "work"
    m.tier = lambda s: (HAIKU, "low", "near_miss", "f")
    s = agents.Session("claude-1", "claude", "c", "demo", "claude-1", {"model": "tiered", "effort": "high"})
    (tmp_path / "w").mkdir()
    cmd, _, _ = m._command(s, "tok", tmp_path / "w")  # before the run picked: the policy is asked
    assert cmd[cmd.index("--model") + 1] == HAIKU and cmd[cmd.index("--effort") + 1] == "low"
    assert "tiered" not in cmd
    s.routed_model, s.routed_effort = OPUS, "high"  # what the run picked wins
    cmd, _, _ = m._command(s, "tok", tmp_path / "w")
    assert cmd[cmd.index("--model") + 1] == OPUS and cmd[cmd.index("--effort") + 1] == "high"
    plain = agents.Session("claude-2", "claude", "c", "demo", "claude-2", {"model": "sonnet"})
    cmd, _, _ = m._command(plain, "tok", tmp_path / "w")
    assert cmd[cmd.index("--model") + 1] == "sonnet" and "--effort" not in cmd  # none chosen: Claude Code's own
    chosen = agents.Session("claude-3", "claude", "c", "demo", "claude-3", {"model": "opus", "effort": "xhigh"})
    cmd, _, _ = m._command(chosen, "tok", tmp_path / "w")  # Work on this picks one (and steps it up when stuck)
    assert cmd[cmd.index("--model") + 1] == "opus" and cmd[cmd.index("--effort") + 1] == "xhigh"
    odd = agents.Session("claude-4", "claude", "c", "demo", "claude-4", {"model": "opus", "effort": "turbo"})
    assert "--effort" not in m._command(odd, "tok", tmp_path / "w")[0]  # only what --effort takes


def test_starting_a_tiered_helper(tmp_path, monkeypatch):
    net, home = _net(tmp_path)
    mcp, manager, security = build(net, home / ".decomp", 6497, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6497")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "demo", "count": 2,
                                          "options": {"model": "tiered", "effort": "max", "per_session": 5}}, headers=h)
    assert r.status_code == 200, r.text
    ids = r.json()["started"]
    assert len(ids) == 2
    for sid in ids:
        s = manager.sessions[sid]
        assert s.tiered and s.options["per_session"] == 1 and "effort" not in s.options  # one function per session
    settings = c.get("/api/state").json()["settings"]
    assert settings["launcher"]["model"] == "tiered"
    assert "tiered" not in settings.get("recent_models", {}).get("claude", [])  # offered already, not a typed name
    # tiers are Claude Code's models: no other agent runs "tiered"
    r = c.post("/api/agents/start", json={"kind": "codex", "slug": "demo", "options": {"model": "tiered"}}, headers=h)
    assert r.status_code == 400 and "Claude Code" in r.json()["error"]
    assert agents.models("claude", None)[0]["id"] == "tiered"  # the start dialog offers it first for Claude Code
