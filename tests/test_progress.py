"""Progress short of matches: each attempt says which setup made it (agent kind, model, effort) and how far its
function was then, so the overview can show who got functions closer and which setup works best on like work."""

import json

from decompnet import scoring
from decompnet.app.agents import Manager, Session
from decompnet.app.engine import RomNetwork

from .test_room_identity import _project


def _fn(net, pid, name, insns=10, status="open"):
    with net.store.tx() as db:
        fid = db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status,attempts) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)", (pid, name, insns * 2, insns, b"\x00" * insns * 2, "[]", "", "",
                                                             0, 0, status)).lastrowid
    return net.store.one("SELECT * FROM functions WHERE id=?", fid)


def _try(net, agent, fid, score):
    """An attempt as compile_and_diff records it (network._record)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=?", fid)
    net._record(fn, agent, score, "int f(void) { return 0; }", 1, matched=score == 0)


def test_an_attempt_says_which_setup_made_it_and_how_far_its_function_was(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    pid = net.project_row(_project(net))["id"]
    fid = _fn(net, pid, "f", insns=10)["id"]
    helper, _ = net.local_agent("claude-1")
    mine, _ = net.local_agent("my-agent")
    net.run_info = lambda agent_id: {"kind": "claude", "model": "sonnet", "effort": "medium"} if agent_id == helper.id else None
    _try(net, helper, fid, 800)  # nothing compiled yet: a first try
    _try(net, helper, fid, 50)   # 80% off before it: far off
    _try(net, mine, fid, 30)     # 5% off before it: a near miss; an agent of the user's own
    rows = net.store.all("SELECT kind, model, effort, stage FROM attempts ORDER BY id")
    assert [tuple(r) for r in rows] == [("claude", "sonnet", "medium", "fresh"), ("claude", "sonnet", "medium", "structural"),
                                         ("own", None, None, "near_miss")]


def test_the_stages_are_the_tiers(tmp_path):
    from decompnet.app import agents

    for best, attempts in ((None, 0), (500, 3), (3000, 3), (8000, 3), (8000, scoring.STALL_ATTEMPTS)):
        state = {"best_score": best, "n_insns": 100, "attempts": attempts}
        assert agents.tier_for(state)[2] == scoring.stage(best, 100, attempts)


def test_progress_shows_who_got_functions_closer_and_which_setup(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    a, b = _fn(net, pid, "a", insns=10)["id"], _fn(net, pid, "b", insns=30)["id"]
    aside = _fn(net, pid, "padding", insns=10, status="asm")["id"]
    codex, _ = net.local_agent("codex-1")
    claude, _ = net.local_agent("claude-1")
    setups = {codex.id: {"kind": "codex", "model": "gpt-5.5", "effort": "high"},
              claude.id: {"kind": "claude", "model": "sonnet", "effort": "medium"}}
    net.run_info = lambda agent_id: setups.get(agent_id)
    _try(net, codex, a, 600)    # a: 0% -> 40%
    _try(net, codex, a, 100)    # a: 40% -> 90%
    _try(net, claude, a, 300)   # worse than a's best: no progress
    _try(net, claude, b, 1500)  # b: 0% -> 50%
    _try(net, claude, b, 0)     # b: 50% -> matched
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE id=?", (b,))
    _try(net, codex, aside, 50)  # set aside: no C makes it, so getting closer to it counts for nothing
    st = net.stats(slug)
    points = {r["agent"]: r["points"] for r in st["agents"]}
    assert points == {"codex-1": 90, "claude-1": 100}
    assert [(c["name"], round(c["from"], 2), round(c["to"], 2), c["agent"]) for c in st["closer"]] == [("a", 0.4, 0.9, "codex-1")]
    # the best attempts reproduce 90% of a (10 instructions) and all of b (30): 39 of 40
    assert round(st["closeness"]["now"], 1) == 97.5 and round(st["closeness"]["today"], 1) == 97.5
    by = {s["kind"]: s for s in st["setups"]}
    assert by["codex"]["model"] == "gpt-5.5" and by["codex"]["attempts"] == 2 and by["codex"]["points"] == 90  # not the aside one
    assert by["claude"]["matches"] == 1 and by["claude"]["attempts"] == 3 and by["claude"]["per_attempt"] == 33.33
    assert by["codex"]["stages"]["fresh"] == {"attempts": 1, "per_attempt": 40.0}
    assert by["codex"]["stages"]["structural"] == {"attempts": 1, "per_attempt": 50.0}


def test_an_attempt_made_while_stats_are_counted_is_this_hours(tmp_path):
    """Agents keep compiling while the overview asks: an attempt newer than the request's `now` counts as this hour,
    and doesn't fail the request (it did: an hour index past the end)."""
    import time

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    fid = _fn(net, pid, "a", insns=10)["id"]
    agent, _ = net.local_agent("codex-1")
    _try(net, agent, fid, 500)
    with net.store.tx() as db:
        db.execute("UPDATE attempts SET created_at=?", (time.time() + 5,))  # saved just after the request began
    st = net.stats(slug)
    assert st["closeness"]["per_hour"][-1] == 50.0 and st["closeness"]["today"] == 50.0


def test_the_helper_running_as_an_agent_is_what_its_attempts_say(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    manager = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    s = Session(id="s1", kind="api", label="api-1", slug="x", agent_name="api-1", options={"model": "deepseek-flash"})
    s.agent_id = 7
    manager.sessions[s.id] = s
    assert net.run_info(7) == {"kind": "api", "model": "deepseek-flash", "effort": ""}
    assert net.run_info(8) is None
    tiered = Session(id="s2", kind="claude", label="claude-1", slug="x", agent_name="claude-1", options={"model": "tiered"})
    tiered.agent_id, tiered.routed_model, tiered.routed_effort = 9, "haiku", "low"
    manager.sessions[tiered.id] = tiered
    assert net.run_info(9) == {"kind": "claude", "model": "haiku", "effort": "low"}  # what it runs this function on
    assert json.dumps(net.run_info(9))  # stored as is
