"""The assembly track through the agents' tools: claimed by track (or by name), compile_and_diff takes the routine's
assembly, and the brief shows the routine, a draft that rebuilds it, what the map knows and the six checks."""

from __future__ import annotations

import pytest

from decompnet.app import tools
from decompnet.network import NetworkError
from decompnet.rom import gb_asm
from tests.test_documented import game  # noqa: F401 (a fixture)
from tests.test_gb_asm import GOOD

pytestmark = pytest.mark.skipif(not gb_asm.available(), reason="needs RGBDS")


def test_claiming_on_the_track_hands_out_hand_written_routines_with_a_draft(game):  # noqa: F811
    net, agent = game
    brief = tools.HANDLERS["claim_function"](net, agent, "g", track="asm")
    assert brief.startswith("## CLAIMED (assembly track): g/Call_002_4a10")
    assert "### A draft to start from" in brief and "call FillTilemapZero" in brief and "### Accepted when" in brief
    assert net.store.one("SELECT agent_id FROM claims")["agent_id"] == agent.id
    # nothing else on the track (the other set-aside one is padding)
    other = net.authenticate(net.register_agent("second"))
    assert "Nothing left on the assembly track" in tools.HANDLERS["claim_function"](net, other, "g", track="asm")


def test_a_routine_asked_for_by_name_comes_on_its_track(game):  # noqa: F811
    net, agent = game
    assert "(assembly track)" in net.claim(agent, "g", "Call_002_4a10")
    with pytest.raises(NetworkError, match="isn't a hand-written routine"):
        net.claim_asm(net.authenticate(net.register_agent("x")), "g", "FillTilemapZero")
    with pytest.raises(NetworkError, match="track is c"):
        net.claim(agent, "g", None, track="pascal")


def test_compile_and_diff_judges_its_assembly(game):  # noqa: F811
    net, agent = game
    tools.HANDLERS["claim_function"](net, agent, "g", track="asm")
    out = tools.HANDLERS["compile_and_diff"](net, agent, "g", "Call_002_4a10", GOOD)
    assert out.startswith("❌ not yet") and "DEF wMenuOpen EQU $XXXX" in out  # not a project name yet
    out = tools.HANDLERS["compile_and_diff"](net, agent, "g", "Call_002_4a10", "DEF wMenuOpen EQU $C0A2\n" + GOOD)
    assert out.startswith("✅ DOCUMENTED: ClearMenuBackground"), out
    assert "DOCUMENTED by" in net.get_function("ClearMenuBackground", "g")
    assert not net.store.one("SELECT 1 FROM claims")


def test_the_guide_has_the_track():
    assert "asm" in tools.GUIDES and "six checks" in tools._guide("asm")
    assert "asm" in tools.PLATFORM_GUIDES["gbc"]


def test_helpers_start_on_the_track_with_its_own_task(game, tmp_path, monkeypatch):  # noqa: F811
    from starlette.testclient import TestClient

    from decompnet.app import agents
    from decompnet.app.server import build

    net, _ = game
    mcp, manager, security = build(net, tmp_path / ".decomp", 6497, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6497")
    c.cookies.set("decomp_live", "k")
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "g", "options": {"track": "asm", "focus": "quick"}},
               headers={"X-Decomp": "1"})
    assert r.status_code == 200, r.text
    s = next(iter(manager.sessions.values()))
    assert s.options["track"] == "asm" and "focus" not in s.options
    assert 'claim_function(track="asm")' in manager._prompt(s) and s.snapshot()["track"] == "asm"
    assert manager._left("g", "asm") == 1 and manager._left("g") == 1  # one routine to document, one open for C
