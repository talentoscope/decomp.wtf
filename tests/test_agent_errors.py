"""What a helper does when its agent says it can't go on: stop for good when retrying can't help (no credit), wait
when it can (a plan's quota resets, a provider's limit of requests), and never sit silent holding a function."""

import json
import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from decompnet.app import agents
from decompnet.app.agents import Manager, PlanLimit, Session, no_credit, plan_limit
from decompnet.app.engine import RomNetwork


def test_an_empty_account_stops_the_helper_and_ordinary_failures_do_not():
    assert "top it up" in no_credit("Error: Insufficient Balance")
    assert no_credit("Error: 429 Too Many Requests") is None  # throttled: waits and tries again
    assert no_credit("exited with code 1: compile failed") is None
    assert no_credit("You've hit your session limit · resets 6pm") is None  # a plan's quota: it waits instead


def test_a_used_up_plan_waits_for_the_reset_the_agent_names():
    now = datetime(2026, 10, 18, 15, 0, tzinfo=ZoneInfo("America/Cordoba")).timestamp()
    hit = plan_limit("You've hit your session limit · resets 6pm (America/Cordoba)", now)
    assert isinstance(hit, PlanLimit) and "resets 6pm" in hit.said
    assert hit.until == now + 3 * 3600 + 60  # 6pm there, and a minute to be sure
    later = plan_limit("You've hit your session limit · resets 2am (America/Cordoba)", now)
    assert later.until == now + 11 * 3600 + 60  # tomorrow's
    codex = plan_limit("You've hit your usage limit. Upgrade to Pro, or try again in 2 hours 13 minutes.", now)
    assert codex.until == now + 2 * 3600 + 13 * 60 + 60
    assert plan_limit("Claude AI usage limit reached|1760000000", now).until == 1760000000
    assert plan_limit("You've hit your weekly limit", now).until == now + agents.QUOTA_WAIT  # no time given
    assert plan_limit("rate limit: 429", now) is None


def _project(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"kind": "rom"})))
        db.execute("INSERT INTO functions(project_id, name, size, n_insns) VALUES(1,'f',8,2)")
    return net


def test_every_helper_on_the_account_waits_for_its_plan_to_reset(tmp_path, monkeypatch):
    net = _project(tmp_path)
    m = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    calls = []

    def run_cli(s, token, workdir):
        calls.append(s.id)
        raise PlanLimit("You've hit your usage limit", time.time() + 3600)
    monkeypatch.setattr(m, "_run_cli", run_cli)
    first = Session("codex-1", "codex", "Codex #1", "game", "codex-1", {})
    second = Session("codex-2", "codex", "Codex #2", "game", "codex-2", {})
    other = Session("api-1", "api", "API #1", "game", "api-1", {"base_url": "http://box/v1"})
    for s in (first, second):
        threading.Thread(target=m._run, args=(s,), daemon=True).start()
        for _ in range(100):
            if s.state == "waiting":
                break
            time.sleep(0.02)
    assert calls == ["codex-1"]  # the second never started a run: the account is waiting
    assert first.state == second.state == "waiting" and second.wait_until == m.paused["codex"]
    assert "usage limit" in second.wait_why and m.account(other) == "api:http://box/v1"
    for s in (first, second):
        s.stop.set()


def test_a_silent_agent_is_ended_and_the_next_session_told_where_it_was(tmp_path, monkeypatch):
    net = _project(tmp_path)
    m = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    agent, _ = net.local_agent("codex-1")
    net.claim_row(agent, "game", "f")
    monkeypatch.setenv("DECOMP_AGENT_IDLE", "1")
    monkeypatch.setattr(m, "_command", lambda s, token, workdir: (
        [sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), ""))
    s = Session("codex-1", "codex", "Codex #1", "game", "codex-1", {})
    s.agent_id, s.runs = agent.id, 1
    t0 = time.time()
    assert m._run_cli(s, "tok", tmp_path) is False
    assert time.time() - t0 < 20 and "said nothing for 1 seconds" in s.error
    assert "f (nothing compiled yet)" in s.resume_note and "claim_function gives it back" in s.resume_note
    assert s.last_output == 0.0  # no run going


def test_an_api_request_that_times_out_is_sent_again(monkeypatch):
    monkeypatch.setattr(agents, "API_RESEND_WAIT", 0.0)
    s = Session("api-1", "api", "API #1", "game", "api-1", {})

    class Flaky:
        def __init__(self, fails):
            self.fails = fails

        def post(self, url, **kw):
            if self.fails:
                self.fails -= 1
                raise httpx.ReadTimeout("slow")
            return "reply"
    assert agents._post(s, Flaky(2), "http://box/v1/chat/completions") == "reply"
    assert sum("sending it again" in f["text"] for f in s.feed) == 2
    with pytest.raises(httpx.ReadTimeout):
        agents._post(s, Flaky(agents.API_RESENDS + 1), "http://box/v1/chat/completions")
