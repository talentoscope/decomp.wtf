"""Disconnecting an outside agent from the Agents tab: what it claimed goes back to the others, and its token is
refused with a message that says why."""

import pytest
from starlette.testclient import TestClient

from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError

PORT, SECRET = 6498, "s3cret-for-disconnect"


def _project(net, tmp_path):
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1, 'game', 'game', ?, '{}')", (str(tmp_path),))
        for fid in (1, 2, 3):
            db.execute("INSERT INTO functions(id, project_id, name) VALUES(?, 1, ?)", (fid, f"f{fid}"))


def _claim(net, agent, fid):
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, 0, 1e12))


@pytest.fixture
def net(tmp_path):
    net = RomNetwork(tmp_path / "decomp.db", tmp_path / "work")
    _project(net, tmp_path)
    return net


def test_disconnecting_frees_its_claims_and_refuses_its_token(net):
    kicked, token = net.chat_agent("claude-code")
    other, _ = net.chat_agent("codex")
    _claim(net, kicked, 1)
    _claim(net, kicked, 2)
    _claim(net, other, 3)

    assert sorted(net.disconnect_agent("claude-code")) == ["f1", "f2"]
    held = net.store.all("SELECT function_id, agent_id FROM claims")
    assert [(r["function_id"], r["agent_id"]) for r in held] == [(3, other.id)]  # only its own claims went
    assert net.store.one("SELECT abandons FROM functions WHERE id=1")["abandons"] == 0  # not the function's fault
    with pytest.raises(NetworkError, match="disconnected by the user"):
        net.authenticate(token)
    with pytest.raises(NetworkError, match="invalid API token"):
        net.authenticate("dn_never-issued")  # a token nobody ever had is still just invalid
    net.disconnect_agent("claude-code")  # twice: still refused, nothing breaks
    with pytest.raises(NetworkError, match="disconnected by the user"):
        net.authenticate(token)


def test_a_disconnected_function_can_be_claimed_by_someone_else(net):
    kicked, _ = net.local_agent("claude-14")
    _claim(net, kicked, 1)
    net.disconnect_agent("claude-14")
    assert net.store.one("SELECT 1 FROM claims WHERE function_id=1") is None
    newcomer, _ = net.chat_agent("opencode")
    _claim(net, newcomer, 1)  # the slot is free: nothing collides


def test_an_agent_connecting_again_gets_a_working_identity(net):
    _, token = net.chat_agent("claude-code")
    net.disconnect_agent("claude-code")
    fresh, new_token = net.chat_agent("claude-code")  # the user starts it again on purpose
    assert net.authenticate(new_token).id == fresh.id
    with pytest.raises(NetworkError):
        net.authenticate(token)  # the old session stays out


def test_only_connected_agents_can_be_disconnected(net):
    net.register_agent("someone", "you", origin="cli")
    for name in ("someone", "nobody", "you", "autopilot"):
        with pytest.raises(NetworkError):
            net.disconnect_agent(name)


def test_disconnect_all_leaves_the_apps_own_helpers_alone(net):
    a, _ = net.chat_agent("claude-code")
    b, _ = net.chat_agent("codex")
    helper, _ = net.local_agent("claude-1")
    for fid, who in ((1, a), (2, b), (3, helper)):
        _claim(net, who, fid)
    assert net.disconnect_all("game", keep={"claude-1"}) == ["claude-code", "codex"]
    assert [r["agent_id"] for r in net.store.all("SELECT agent_id FROM claims")] == [helper.id]


@pytest.fixture
def api(tmp_path):
    net = RomNetwork(tmp_path / "decomp.db", tmp_path / "work")
    _project(net, tmp_path)
    mcp, manager, security = build(net, tmp_path / ".decomp", PORT, SECRET)
    (tmp_path / ".decomp").mkdir(exist_ok=True)
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("decomp_live", SECRET)
    return client, net, manager


def test_the_agents_tab_can_disconnect_one_or_all(api):
    client, net, manager = api
    h = {"X-Decomp": "1"}
    a, ta = net.chat_agent("claude-code")
    b, tb = net.chat_agent("codex")
    _claim(net, a, 1)
    _claim(net, b, 2)
    assert client.post("/api/agents/disconnect", json={"name": "claude-code"}).status_code == 403  # needs the header
    r = client.post("/api/agents/disconnect", json={"name": "claude-code"}, headers=h)
    assert r.status_code == 200 and r.json()["released"] == ["f1"]
    with pytest.raises(NetworkError, match="disconnected"):
        net.authenticate(ta)
    assert client.post("/api/agents/disconnect", json={"name": "ghost"}, headers=h).status_code == 400
    r = client.post("/api/agents/disconnect", json={"slug": "game"}, headers=h)
    assert r.status_code == 200 and r.json()["disconnected"] == ["codex"]
    with pytest.raises(NetworkError, match="disconnected"):
        net.authenticate(tb)
    assert net.store.one("SELECT 1 FROM claims") is None


def test_remove_all_clears_only_the_stopped_helper_cards_of_a_project(api):
    client, net, manager = api
    from decompnet.app import agents
    for sid, state, slug in (("claude-1", "stopped", "game"), ("claude-2", "error", "game"), ("claude-3", "working", "game"),
                             ("claude-4", "stopped", "other")):
        s = agents.Session(sid, "claude", sid, slug, sid, {})
        s.state = state
        manager.sessions[sid] = s
    r = client.post("/api/agents/stop", json={"slug": "game", "remove": True}, headers={"X-Decomp": "1"})
    assert r.status_code == 200
    assert sorted(manager.sessions) == ["claude-3", "claude-4"]  # the one still working, and another project's
