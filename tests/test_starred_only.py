"""A helper launched for the ★ Next in line functions only: it gets those, nothing else, and is told when they're done."""

import json
import time

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents
from decompnet.app.agents import Manager
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError


@pytest.fixture
def net(tmp_path):
    net = RomNetwork(tmp_path / "decomp.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"priority": ["star_b", "star_a"]})))
        for name in ("easy", "star_a", "star_b"):  # `easy` would be everybody's first pick
            db.execute("INSERT INTO functions(project_id, name, size, n_insns) VALUES(1,?,?,?)",
                       (name, 4, 1 if name == "easy" else 50))
    return net


def test_a_starred_only_helper_takes_the_stars_in_order_and_nothing_else(net):
    helper, _ = net.local_agent("helper")
    net.starred_only[helper.id] = 0.0

    with pytest.raises(NetworkError, match="isn't marked"):
        net.claim_row(helper, "game", "easy")  # by name, but not starred

    first = net.claim_row(helper, "game")
    assert first["name"] == "star_b"  # the pin list's order, not the easiest first
    net.release(helper, "star_b", "", "game")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='star_b'")
    assert net.claim_row(helper, "game")["name"] == "star_a"
    net.release(helper, "star_a", "", "game")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='star_a'")

    with pytest.raises(NetworkError, match="no ★ Next in line function is left for you"):
        net.claim_row(helper, "game")  # all starred are done: it doesn't fall back to `easy`


def test_other_helpers_are_not_limited_to_the_stars(net):
    other, _ = net.local_agent("other")
    assert net.claim_row(other, "game", "easy")["name"] == "easy"


def test_a_starred_only_helper_can_renew_the_claim_it_holds(net):
    helper, _ = net.local_agent("helper")
    net.starred_only[helper.id] = 0.0
    held = net.claim_row(helper, "game")
    with net.store.tx() as db:  # unpinned while it works on it
        db.execute("UPDATE projects SET config=?", (json.dumps({"priority": []}),))
    assert net.claim_row(helper, "game", held["name"])["name"] == held["name"]


def test_the_session_knows_how_many_stars_are_left_and_how_many_are_free_to_take(net, tmp_path):
    manager = Manager(net, tmp_path, "http://127.0.0.1:0/mcp")
    helper, _ = net.local_agent("helper")
    other, _ = net.local_agent("other")
    assert manager._starred_left("game", helper.id) == (2, 2)

    net.claim_row(other, "game", "star_b")  # somebody else is on one of them
    assert manager._starred_left("game", helper.id) == (2, 1)

    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name IN ('star_a','star_b')")
    assert manager._starred_left("game", helper.id) == (0, 0)  # done: the session ends


def test_a_star_the_helper_gave_back_is_not_handed_to_it_again(net, tmp_path):
    """It was found to be data, not code: the next claim moves on, and when nothing else is left the session ends
    (without this the same star came back every time and a new paid session started for it)."""
    manager = Manager(net, tmp_path, "http://127.0.0.1:0/mcp")
    helper, _ = net.local_agent("helper")
    began = time.time() - 1
    net.starred_only[helper.id] = began

    assert net.claim_row(helper, "game")["name"] == "star_b"
    net.release(helper, "star_b", "that's data", "game")
    assert net.claim_row(helper, "game")["name"] == "star_a"  # not star_b again
    net.release(helper, "star_a", "data too", "game")
    assert manager._starred_left("game", helper.id, began) == (0, 0)  # nothing worth trying: the session ends
    with pytest.raises(NetworkError, match="no ★ Next in line function is left for you"):
        net.claim_row(helper, "game")
    assert manager._starred_left("game", helper.id, time.time() + 5) == (2, 2)  # a later session tries them anew


def test_a_match_takes_the_function_off_next_in_line(net):
    agent, _ = net.local_agent("helper")
    fn = net.claim_row(agent, "game", "star_a")
    net._record(fn, agent, 0, "int star_a(void) { return 0; }", 1, matched=True)
    assert net.cfg(net._project("game"))["priority"] == ["star_b"]


def test_the_list_never_shows_a_function_that_is_already_done(net):
    with net.store.tx() as db:  # matched some other way (a twin, a rescan...), still in the saved list
        db.execute("UPDATE functions SET status='matched' WHERE name='star_b'")
    assert net._open_priority(net._project("game"), ["star_b", "star_a"]) == ["star_a"]


def test_clearing_claims_touches_only_that_project(tmp_path):
    from decompnet.network import Network

    n = Network(tmp_path / "other.db")
    with n.store.tx() as db:
        for pid, slug in ((1, "one"), (2, "two")):
            db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(?,?,?,?,?)",
                       (pid, slug, slug, str(tmp_path), "{}"))
        for fid, pid in ((1, 1), (2, 1), (3, 2)):
            db.execute("INSERT INTO functions(id, project_id, name) VALUES(?,?,?)", (fid, pid, f"f{fid}"))
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, 7, 0, 1e12))
    assert n.clear_claims("one") == 2
    assert [r["function_id"] for r in n.store.all("SELECT function_id FROM claims")] == [3]
    assert n.clear_claims("one") == 0  # nothing left to cut
    with pytest.raises(NetworkError):
        n.clear_claims("missing")


def test_starting_a_starred_only_helper(net, tmp_path, monkeypatch):
    mcp, manager, security = build(net, tmp_path / ".decomp", 6496, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6496")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "game", "options": {"only_starred": True}}, headers=h)
    assert r.status_code == 200, r.text
    (sid,) = r.json()["started"]
    assert manager.sessions[sid].options["only_starred"] is True and manager.sessions[sid].snapshot()["only_starred"]
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "game", "options": {"only_starred": "yes"}}, headers=h)
    (sid2,) = r.json()["started"]
    assert "only_starred" not in manager.sessions[sid2].options  # only a real true
    assert c.post("/api/claims/clear", json={"slug": "game"}, headers=h).json() == {"cleared": 0}


def test_a_star_given_up_isnt_handed_straight_back(net):
    """One fork measured 240 claims on 8 stars in 3 hours, nothing matched, while 800 functions waited: a helper got
    back the star it had just released, every time."""
    first, _ = net.local_agent("first")
    assert net.claim_row(first, "game")["name"] == "star_b"
    net.release(first, "star_b", "data, not code", "game")
    assert net.claim_row(first, "game")["name"] == "star_a"  # not star_b again
    second, _ = net.local_agent("second")
    assert net.claim_row(second, "game")["name"] == "easy"  # star_b was given up minutes ago: it waits its turn
    with net.store.tx() as db:  # an hour later, someone else may try it
        db.execute("UPDATE events SET created_at=created_at-3600 WHERE kind='release'")
    net.release(second, "easy", "", "game")
    third, _ = net.local_agent("third")
    assert net.claim_row(third, "game")["name"] == "star_b"
