"""API helpers' time on a routine: ROUTINE_BUDGET in all, no resend after a timeout, a reply cap from the model's
measured speed, and a nudge when a reply runs out of room while the model is still thinking."""
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from decompnet.app import agents


def test_reply_cap_comes_from_the_measured_speed():
    s = agents.Session("api-1", "api", "API #1", "demo", "api-1", {})
    assert agents.reply_cap(s, 300) is None  # speed not known yet: the timeout bounds the reply
    agents.measure_rate(s, 1400, 100.0)
    assert s.tok_rate == pytest.approx(14.0)
    assert agents.reply_cap(s, 300) == int(14 * 300 * 0.9)
    assert agents.reply_cap(s, 5) == 256
    agents.measure_rate(s, 2000, 100.0)  # smoothed, not replaced
    assert 14 < s.tok_rate < 20
    s.options["max_tokens"] = 1000
    assert agents.reply_cap(s, 300) == 1000
    agents.measure_rate(s, 0, 100.0)
    agents.measure_rate(s, 50, 0.2)  # nothing to learn from these
    assert 14 < s.tok_rate < 20


def test_a_timed_out_request_is_not_sent_again():
    calls = []

    class Client:
        def post(self, url, **kw):
            calls.append(kw.get("timeout"))
            raise httpx.ReadTimeout("slow")

    s = agents.Session("api-1", "api", "API #1", "demo", "api-1", {})
    with pytest.raises(httpx.ReadTimeout):
        agents._post(s, Client(), "http://box/v1/chat/completions", json={}, timeout=5)
    assert calls == [5]


class _Model:
    """A stand-in model server: each request gets the next of `replies` (a dict, or a number of seconds to hang)."""

    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.bodies.append(body)
                reply = model.replies.pop(0) if model.replies else {"content": "done", "finish_reason": "stop"}
                if isinstance(reply, (int, float)):
                    time.sleep(reply)
                    reply = {"content": "too late", "finish_reason": "stop"}
                time.sleep(reply.get("delay", 0))
                out = json.dumps({"model": "m", "usage": {"total_tokens": 100, "completion_tokens": 50},
                                  "choices": [{"finish_reason": reply.get("finish_reason", "stop"),
                                               "message": {"role": "assistant", "content": reply.get("content", ""),
                                                           "tool_calls": reply.get("tool_calls")}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()


def _project(tmp_path):
    from decompnet.app.engine import RomNetwork
    # 10 SYS 2061, and at $080D (2061): lda #$01 / sta $FB / rts
    line = struct.pack("<H", 10) + b"\x9e2061\x00"
    program = struct.pack("<H", 0x0801 + 2 + len(line)) + line + b"\x00\x00"
    code = program + bytes(0x080D - 0x0801 - len(program)) + bytes.fromhex("a90185fb60")
    path = tmp_path / "demo.prg"
    path.write_bytes(struct.pack("<H", 0x0801) + code)
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(path, confirmed=True, log=lambda *_: None)
    return net, slug


def _session(m, net, slug, base, name="api-1"):
    agent, _token = net.local_agent(name)
    s = agents.Session(name, "api", name, slug, name, {"base_url": base, "model": "m", "track": "asm",
                                                     "context_tokens": 32000, "max_steps": 6})
    s.agent_id = agent.id
    m.sessions[s.id] = s
    return s, agent


def test_a_reply_that_runs_out_of_room_gets_a_nudge_not_a_wait(tmp_path):
    net, slug = _project(tmp_path)
    model = _Model([{"content": "", "finish_reason": "length", "delay": 1.2}, {"content": "ok", "finish_reason": "stop"}])
    try:
        m = agents.Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
        s, agent = _session(m, net, slug, model.base)
        m._run_api(s, agent)
        assert len(model.bodies) == 2
        assert model.bodies[1]["messages"][-1] == {"role": "user", "content": agents.CAPPED}
        assert "max_tokens" in model.bodies[1]  # its speed was measured on the first reply
        assert any("hit" in f["text"] and "still thinking" in f["text"] for f in s.feed)
    finally:
        model.close()


def test_a_routine_out_of_time_goes_back_then_is_marked_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMP_ROUTINE_BUDGET", "2")
    monkeypatch.setattr(agents, "ROUTINE_SLACK", 0)
    net, slug = _project(tmp_path)
    model = _Model([6, {"content": "ok"}, 6, {"content": "ok"}])
    try:
        m = agents.Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
        s, agent = _session(m, net, slug, model.base)
        brief = net.claim_asm(agent, slug)
        name = brief.splitlines()[0].split("/")[-1]
        took = time.time()
        m._run_api(s, agent)
        assert time.time() - took < 4, "the request waited past the routine's time"
        fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
        assert net.store.one("SELECT 1 FROM claims WHERE function_id=?", fn["id"]) is None and not fn["ignored"]
        assert net.store.one("SELECT text FROM notes WHERE function_id=?", fn["id"])["text"].startswith(agents.OUT_OF_TIME)
        told = [x for x in model.bodies[1]["messages"] if x["role"] == "user"][-1]["content"]
        assert told.startswith(f"Time's up on {name}") and "claim_function" in told
        assert len(model.bodies) == 2  # the timed-out request wasn't sent again

        net.claim_asm(agent, slug)
        m._run_api(s, agent)
        fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
        assert fn["ignored"] and net.store.one("SELECT 1 FROM claims WHERE function_id=?", fn["id"]) is None
        assert any("marked it stuck" in f["text"] for f in s.feed)
    finally:
        model.close()


def test_stop_ends_a_request_waiting_on_the_model(tmp_path):
    net, slug = _project(tmp_path)
    model = _Model([30])
    try:
        m = agents.Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
        s, agent = _session(m, net, slug, model.base)
        net.claim_asm(agent, slug)
        done = []
        t = threading.Thread(target=lambda: done.append(m._run_api(s, agent)), daemon=True)
        t.start()
        for _ in range(50):
            if s.asking_since:
                break
            time.sleep(0.1)
        assert s.asking_since and s.snapshot()["asking_since"] == s.asking_since
        assert s.snapshot()["routine_until"] > time.time()
        took = time.time()
        m.stop(s.id)
        t.join(10)
        assert not t.is_alive() and time.time() - took < 5, "Stop waited for the model"
    finally:
        model.close()
