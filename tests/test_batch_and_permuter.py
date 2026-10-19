"""A batch of small functions compiles once (the build holds every function it defines), and the permuter keeps
count of its turns across restarts and says when its rewrites don't compile."""

import json

from decompnet.app.engine import RomNetwork
from decompnet.app.permute import MAX_TURNS, Permuter, State
from decompnet.compiler import CompileResult


def _net(tmp_path, n=3):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"kind": "rom", "compiler": {"toolchain": "gbdk-2020"}})))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,1,'',NULL,'',0,'')")
        for i in range(n):
            db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, best_score, best_source) "
                       "VALUES(1,?,8,10,?,'[]',?,?)", (f"f{i}", b"\0" * 8, 20 + i, f"void f{i}(void) {{}}"))
    return net


def test_a_batch_of_functions_compiles_once(tmp_path, monkeypatch):
    net = _net(tmp_path)
    compiles = []

    def compile_now(proj, header, source):
        compiles.append(source)
        return CompileResult(False, None, "func.c:1: syntax error")
    monkeypatch.setattr(net, "_compile_now", compile_now)
    agent, _ = net.local_agent("claude-1")
    with net.store.tx() as db:  # claimed as a batch (claim_function count=3)
        db.executemany("INSERT INTO claims VALUES((SELECT id FROM functions WHERE name=?),?,0,9e12)",
                       [(f"f{i}", agent.id) for i in range(3)])
    out = net.attempt_many(agent, ["f0", "f1", "f2"], "void f0(void) {}\nvoid f1(void) {}\nvoid f2(void) {", "game")
    assert out.count("COMPILE ERROR") == 3 and len(compiles) == 1  # three judged, one build
    net.release(agent, "f2", "", "game")
    net.compile_and_diff(agent, "f0", "void f0(void) { }", "game")
    net.release(agent, "f0", "", "game")
    net.compile_and_diff(agent, "f1", "void f0(void) { }", "game")
    assert len(compiles) == 3  # outside a batch, each compile is its own


def test_the_permuters_turns_are_kept_and_start_over_when_the_best_improves(tmp_path):
    net = _net(tmp_path, n=25)
    proj = net.project_row("game")
    spent = net.store.all("SELECT * FROM functions WHERE project_id=1 ORDER BY best_score LIMIT 20")
    for fn in spent:
        for _ in range(MAX_TURNS):
            net.permuter._spend_turn(fn)
    restarted = Permuter(net)  # the app started again: the count is in the database
    assert {r["name"] for r in restarted.candidates(proj)} == {f"f{i}" for i in range(20, 25)}  # not hidden by the spent
    with net.store.tx() as db:  # an agent got f0 closer: worth new turns
        db.execute("UPDATE functions SET best_score=15 WHERE name='f0'")
    assert "f0" in {r["name"] for r in restarted.candidates(proj)}
    assert restarted._spend_turn(net._function("f0", "game")) == 1


def test_rewrites_that_dont_compile_are_counted_with_their_error(tmp_path, monkeypatch):
    net = _net(tmp_path, n=1)
    monkeypatch.setattr(net, "_compile_now", lambda proj, header, source: CompileResult(
        False, None, "\nfunc.c:3: error 20: Undefined identifier 'tmp'\nmore"))
    st = State()
    agent, _ = net.local_agent("permuter")
    fn = net._function("f0", "game")
    assert net.permuter._score(agent, net.project_row("game"), fn, "void f0(void) { tmp; }", 20, None, st) is None
    assert st.failed == 1 and st.last_error == "func.c:3: error 20: Undefined identifier 'tmp'"
