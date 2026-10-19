"""The helpers' Focus menu (Quick Wins, Close Calls): a helper started with a focus claims by it, whatever strategy
it asks for, and stops when that focus has nothing left. Stub-based: no compiler needed."""

import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents, tools
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import QUICK_INSNS

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path)
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1, 'fixture', 'fixture', ?, ?)",
                   (str(tmp_path), json.dumps({"kind": "rom", "priority": ["mid_fn"]})))
        rows = [  # name, instructions, best score (None: never compiled)
            ("small_fn", 20, None),      # a Quick Win: small, untouched
            ("big_far_fn", 200, 15000),  # big, 75% off: nobody's focus
            ("close_fn", 100, 500),      # a Close Call: 95% matched
            ("mid_fn", 100, 5000),       # 50% off: default claiming only (and pinned)
        ]
        for i, (name, n_insns, best) in enumerate(rows, start=1):
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, best_score) VALUES(?,1,?,?,?,?)",
                       (i, name, n_insns * 4, n_insns, best))
    return n


def agent(net, name):
    return net.authenticate(net.register_agent(name))


def test_each_focus_counts_only_its_own_functions(net):
    assert net.strategy_left("fixture", "quick") == 1   # small_fn
    assert net.strategy_left("fixture", "close") == 1   # close_fn
    assert net.strategy_left("fixture", "easiest") == 4
    assert 20 <= QUICK_INSNS < 100


def test_quick_wins_claims_the_small_one_and_close_calls_the_near_miss(net):
    alice, bob = agent(net, "alice"), agent(net, "bob")
    net.focus[alice.id], net.focus[bob.id] = "quick", "close"  # a pin must not pull a focused helper off its rule
    assert net.claim_row(alice, "fixture", None, "quick")["name"] == "small_fn"
    assert net.claim_row(bob, "fixture", None, "close")["name"] == "close_fn"
    # with those taken, each focus is out of work for a third helper, while default claiming still has plenty
    assert net.strategy_left("fixture", "quick") == 0 and net.strategy_left("fixture", "close") == 0
    assert net.strategy_left("fixture", "easiest") == 2
    # the helper that holds the function still counts it as its own work
    assert net.strategy_left("fixture", "quick", alice.id) == 1
    carol = agent(net, "carol")
    net.focus[carol.id] = "quick"
    assert net.claim_row(carol, "fixture", None, "quick") is None  # nothing small left for her


def test_a_pin_still_comes_first_without_a_focus_but_not_with_one(net):
    plain, focused = agent(net, "plain"), agent(net, "focused")
    assert net.claim_row(plain, "fixture")["name"] == "mid_fn"  # the ★ pin, as before
    net.focus[focused.id] = "quick"
    assert net.claim_row(focused, "fixture", None, "quick")["name"] == "small_fn"


def test_a_helpers_focus_overrides_the_strategy_an_agent_asks_for(net, monkeypatch):
    seen = []
    monkeypatch.setattr(net, "claim", lambda a, slug, function=None, strategy="easiest": seen.append(("one", strategy)) or "ok")
    monkeypatch.setattr(net, "claim_many", lambda a, slug, count, strategy="easiest": seen.append(("many", strategy, count)) or "ok")
    carol = agent(net, "carol")
    net.focus[carol.id] = "close"
    tools._claim(net, carol, "fixture", "", "easiest", count=4)  # asks for a batch of the easiest
    assert seen == [("one", "close")]  # a Close Call never adds a batch of neighbours
    net.focus[carol.id] = "quick"
    tools._claim(net, carol, "fixture", "", "easiest", count=4)
    assert seen[-1] == ("many", "quick", 4)  # quick wins may come in a batch
    net.focus.pop(carol.id)
    tools._claim(net, carol, "fixture", "", "", count=1)
    assert seen[-1] == ("one", "easiest")  # no focus: as always


def test_the_focus_table_matches_the_menu_in_the_web_page():
    js = (ROOT / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    menu = set(re.findall(r'\{ id: "(\w+)", name: "[^"]+"', js.split("const FOCUS = ", 1)[1].split("];", 1)[0]))
    assert menu == {"default", *agents.FOCUSES}
    for strategy, left_text, label in agents.FOCUSES.values():
        assert strategy in ("quick", "close") and left_text and label


def test_starting_helpers_with_a_focus(net, tmp_path, monkeypatch):
    mcp, manager, security = build(net, tmp_path / ".decomp", 6495, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6495")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"focus": "close"}}, headers=h)
    assert r.status_code == 200, r.text
    (sid,) = r.json()["started"]
    assert manager.sessions[sid].options["focus"] == "close" and manager.sessions[sid].snapshot()["focus"] == "close"
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"focus": "nope"}}, headers=h)
    assert r.status_code == 400 and "unknown focus" in r.json()["error"]
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='small_fn'")  # no Quick Win left
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"focus": "quick"}}, headers=h)
    assert r.status_code == 400 and "Nothing to start on" in r.json()["error"]
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"focus": "default"}}, headers=h)
    (sid2,) = r.json()["started"]
    assert "focus" not in manager.sessions[sid2].options  # the default leaves claiming alone
