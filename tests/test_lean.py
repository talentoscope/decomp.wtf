"""A helper pinned to one function: the lean loop asks for the function only, in a short prompt that doesn't grow,
keeps what gets closer, says what still differs (summed up when it repeats), steers when calls stop gaining, and
stamps each attempt with how it was asked for and what it cost, so one or several versions per call can be compared."""

import pytest

from decompnet.app import lean
from decompnet.app.agents import Manager, Session
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

from .test_progress import _fn
from .test_room_identity import _project

# what a near miss's diff looked like on a real cartridge: one register, seven times
ROWS = [(" ", "ld a, $80", "ld a, $80"), ("i", "ld b, $10", "ld c, $10"), ("i", "dec b", "dec c"),
        ("i", "ld b, $40", "ld c, $40")] + [("i", "dec b", "dec c")] * 4 + [
       ("<", "push bc", ""), (">", "", "ld a, e"), ("o", "inc hl", "inc hl"), (" ", "ret", "ret")]


def test_what_still_differs_is_summed_up():
    lines = lean.differences(ROWS)
    assert lines[0].startswith("- 7x: the target has `b` where yours has `c`")
    assert "1 instruction only in the target: `push bc`" in "\n".join(lines)
    assert "1 instruction only in yours: `ld a, e`" in "\n".join(lines)
    assert "1 instruction in another order" in "\n".join(lines)


def test_versions_are_read_from_the_reply_and_repeats_known():
    reply = ("Here you go:\n```c\n// idea: a do-while\nvoid f(void) { do { x--; } while (x); }\n```\n"
             "```c\n/* idea: a for loop */\nvoid f(void) { for (; x; x--) ; }\n```\n```c\nint helper(void);\n```")
    got = lean.extract(reply, "f", 3)
    assert len(got) == 2 and lean.idea_of(got[0]) == "a do-while" and lean.idea_of(got[1]) == "a for loop"
    assert lean.extract(reply, "f", 1) == got[:1]
    assert lean.extract("void f(void) { }", "f", 1) == ["void f(void) { }"]  # no fences
    assert lean.extract("I can't help with that.", "f", 1) == []
    assert lean.same_code("void f(void) {\n  x = 1; // set\n}") == lean.same_code("void f(void){x=1;}")


def test_the_model_reads_only_the_declarations_it_can_use():
    header = ("#include \"types.h\"\ntypedef unsigned char u8;\ntypedef struct { u8 x, y; } Pos;\n"
              "#define MAX_HP 99\n#define SPEED 3\nextern u8 wPlayerHP;\nextern u8 wEnemyHP;\n"
              "struct Actor { u8 hp; };\nvoid DrawSprite(u8 id);\nvoid PlaySound(u8 id);\n")
    got = lean.trim_header(header, {"wPlayerHP", "MAX_HP", "DrawSprite", "Actor"})
    assert "typedef unsigned char u8;" in got and "extern u8 wPlayerHP;" in got and "#define MAX_HP 99" in got
    assert "void DrawSprite(u8 id);" in got and "struct Actor" in got
    assert "wEnemyHP" not in got and "PlaySound" not in got and "SPEED" not in got and "#include" not in got
    assert "Pos" not in got  # a struct type nothing here uses


class Script:
    """A model that answers from a list, and remembers what it was asked."""

    def __init__(self, replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, messages, temperature, switch):
        self.asked.append({"text": messages[-1]["content"], "temperature": temperature, "switch": switch})
        return self.replies.pop(0) if self.replies else "```c\nvoid f(void) { }\n```", 1000, "model-b" if switch else "model-a"


def _loop(tmp_path, monkeypatch, replies, scores, **kw):
    """A lean loop on a function `f` whose compiles score from `scores` (source text -> score; None: no compile)."""
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "f", insns=10)
    agent, _ = net.local_agent("api-1")
    stamps = []

    def attempt(agent_, name, source, project):
        score = next((v for k, v in scores.items() if k in source), 500)
        fn = net._function(name, project)
        net._record(fn, agent_, score, source, 1, matched=score == 0)
        return {"text": "error: no" if score is None else f"score {score}", "score": score, "matched": score == 0,
                "rows": ROWS}

    monkeypatch.setattr(net, "attempt", attempt)
    monkeypatch.setattr(lean, "preview", lambda net_, fn, src: ROWS)
    monkeypatch.setattr(lean.Lean, "stable", lambda self, fn: ["# f: the target"])
    model = Script(replies)
    now = {}  # what the session tells its attempts (Manager.run_info does this for real helpers)

    def stamp(method, tokens, model_):
        stamps.append((method, tokens, model_))
        now.update(method=method, tokens=tokens, model=model_)

    net.run_info = lambda agent_id: {"kind": "api", "model": now.get("model"), "effort": "", "method": now.get("method"),
                                     "tokens": now.get("tokens")}
    loop = lean.Lean(net, agent, slug, "f", model, stamp=stamp, **kw)
    return net, loop, model, stamps


def _c(body, idea=""):
    return f"```c\n// idea: {idea or body}\nvoid f(void) {{ {body} }}\n```"


def test_both_methods_in_turn_keep_the_closest_and_say_what_each_cost(tmp_path, monkeypatch):
    net, loop, model, stamps = _loop(tmp_path, monkeypatch, [
        _c("x = 1;"),                                           # one version: score 400
        _c("x = 1;") + _c("x = 2;") + _c("y = 1;"),             # several: a repeat, a compile error, closer
        _c("y = 2;"),                                           # one version: the match
    ], {"x = 1;": 400, "x = 2;": None, "y = 1;": 120, "y = 2;": 0}, method=lean.BOTH, variants=3)
    assert loop.round() is None and loop.round() is None
    assert loop.round() == "f matched 🎉"
    rows = net.store.all("SELECT source, score, method, tokens FROM attempts ORDER BY id")
    assert [(r["method"], r["tokens"]) for r in rows] == [("1 per call", 1000), ("3 per call", 500), ("3 per call", 500),
                                                          ("1 per call", 1000)]  # (the repeat wasn't compiled)
    assert [m for m, _, _ in stamps if m] == ["1 per call", "3 per call", "3 per call", "1 per call"]
    second, third = model.asked[1]["text"], model.asked[2]["text"]
    assert "Reply with 3 complete versions of f" in second and "Reply with one complete version of f" in third
    assert "## Closest so far: score 400" in second and "7x: the target has `b` where yours has `c`" in second
    assert "the same as an earlier version (not compiled)" in third and "x = 2;: didn't compile" in third
    assert "## Closest so far: score 120" in third
    assert net.store.one("SELECT status FROM functions WHERE name='f'")["status"] == "matched"


def test_calls_that_stop_gaining_are_steered_then_stopped(tmp_path, monkeypatch):
    net, loop, model, _ = _loop(tmp_path, monkeypatch, [_c(f"x = {i};") for i in range(10)],
                                {"x = 0;": 300}, stall=5, can_switch=True)
    reasons = [loop.round() for _ in range(6)]  # the first call gains (nothing compiled before), the rest don't
    assert reasons == [None] * 5 + ["no closer in 5 calls: stopping at 70.0%"]
    asked = model.asked
    assert "got no closer" not in asked[2]["text"] and "The last 2 calls got no closer" in asked[3]["text"]
    assert [a["temperature"] for a in asked] == [0.3, 0.3, 0.3, 0.7, 0.7, 0.7]
    assert [a["switch"] for a in asked] == [False, False, False, False, True, True]  # a stronger model after 3
    assert "Pinned helper, 6 calls: from nothing to 70.0%" in loop.summary()


def test_a_function_matched_or_set_aside_meanwhile_ends_the_loop(tmp_path, monkeypatch):
    net, loop, model, _ = _loop(tmp_path, monkeypatch, [], {})
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='asm' WHERE name='f'")
    assert loop.round() == "f isn't open any more (asm)" and not model.asked


def test_a_pinned_helper_claims_its_function_and_only_it(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "easy", insns=2)
    _fn(net, pid, "hard", insns=40)
    agent, _ = net.local_agent("codex-1")
    net.pinned[agent.id] = "hard"
    assert net.claim_row(agent, slug)["name"] == "hard"  # not the easiest
    with pytest.raises(NetworkError, match="works on hard only"):
        net.claim_row(agent, slug, "easy")
    from decompnet.app import tools

    with net.store.tx() as db:
        db.execute("DELETE FROM claims")
    tools.call(net, agent, slug, "claim_function", {"count": 4})  # a batch asked for: the pinned one alone
    assert [r["name"] for r in net.store.all("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id")] == ["hard"]


def test_pinned_sessions_say_how_their_attempts_were_made(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    manager = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    with pytest.raises(ValueError, match="lean loop is for API helpers"):
        manager.start("claude", "x", 1, {"pin": "f", "lean": "one"})
    s = Session(id="s1", kind="api", label="gpu #1", slug="x", agent_name="api-1",
                options={"model": "qwen", "pin": "f", "lean": "both"})
    s.agent_id = 7
    manager.sessions[s.id] = s
    s.method, s.call_tokens, s.using_model = "3 per call", 640, "qwen-big"
    assert net.run_info(7) == {"kind": "api", "model": "qwen-big", "effort": "", "method": "3 per call", "tokens": 640}
    own = Session(id="s2", kind="codex", label="Codex #1", slug="x", agent_name="codex-1", options={"pin": "f", "model": "m", "effort": "high"})
    own.agent_id = 8
    manager.sessions[own.id] = own
    assert net.run_info(8)["method"] == "agent" and "tokens" not in net.run_info(8)
    assert "escalate" not in str(manager.running())


def test_setups_are_compared_by_method_and_tokens(tmp_path, monkeypatch):
    net, loop, _, _ = _loop(tmp_path, monkeypatch, [_c("x = 1;"), _c("y = 1;") + _c("y = 2;") + _c("y = 3;")],
                            {"x = 1;": 600, "y = 1;": 300, "y = 2;": 200, "y = 3;": None}, method=lean.BOTH)
    loop.round()
    loop.round()
    by = {s["method"]: s for s in net.stats(net.store.one("SELECT slug FROM projects")["slug"])["setups"]}
    assert by["1 per call"]["attempts"] == 1 and by["1 per call"]["points"] == 40
    assert by["3 per call"]["attempts"] == 3 and by["3 per call"]["points"] == 40  # 40% -> 80%; a compile error counts
    assert by["1 per call"]["tokens"] == 1000 and by["3 per call"]["tokens"] == 999  # (1000 shared by three)


@pytest.mark.skipif(not __import__("decompnet.rom.gb_build", fromlist=["x"]).available(),
                    reason="needs the GBDK-2020 toolchain (Setup installs it)")
def test_a_pinned_api_helper_gets_a_real_function_to_a_match(tmp_path, monkeypatch):
    """Through the session code the app runs, on a Game Boy function compiled by SDCC: one version, then several,
    the closest kept, the permuter between calls, the match recorded with how it was asked for and what it cost."""
    from decompnet.app import agents
    from tests.test_gb import make_rom

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=bytes.fromhex("ea0021c9")))  # ld ($2100), a; ret
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    pid = net.project_row(slug)["id"]
    name = net.store.one("SELECT name FROM functions WHERE project_id=? AND target=?", pid, bytes.fromhex("ea0021c9"))["name"]
    T = "typedef unsigned char u8;\n"
    replies = [f"```c\n// idea: a write\n{T}void {name}(u8 v) {{ *(volatile u8 *)0x2000 = v; }}\n```",
               f"```c\n// idea: broken\n{T}void {name}(u8 v) {{ oops }}\n```\n"
               f"```c\n// idea: the bank register\n{T}void {name}(u8 v) {{ *(volatile u8 *)0x2100 = v; }}\n```"]
    asked = []

    def chat(s, client, opts, messages, temperature, max_tokens):
        asked.append(messages[-1]["content"])
        return replies.pop(0), 800, opts["model"]

    monkeypatch.setattr(agents, "_chat", chat)
    manager = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    agent, _ = net.local_agent("api-1")
    s = Session(id="api-1", kind="api", label="box #1", slug=slug, agent_name="api-1",
                options={"pin": name, "lean": "both", "variants": 2, "stall": 5, "polish": True, "polish_seconds": 1,
                         "base_url": "http://box/v1", "model": "qwen"})
    s.agent_id = agent.id
    manager.sessions[s.id] = s
    net.pinned[agent.id] = name
    assert manager._run_lean(s, agent)
    assert s.pin_done == f"{name} matched 🎉"
    assert "Reply with 2 complete versions" in asked[1] and "## What still differs" in asked[1]
    assert net.store.one("SELECT status FROM functions WHERE name=?", name)["status"] == "matched"
    made = net.store.all("SELECT method, tokens, model, score FROM attempts WHERE agent_id=? ORDER BY id", agent.id)
    assert [(r["method"], r["tokens"], r["model"]) for r in made] == [("1 per call", 800, "qwen"), ("2 per call", 400, "qwen"),
                                                                      ("2 per call", 400, "qwen")]
    assert made[0]["score"] > 0 and made[1]["score"] is None and made[2]["score"] == 0
    assert s.tokens == 1600


def test_a_pin_start_is_checked(tmp_path):
    from decompnet.app.server import _pin_options

    class Settings:
        def endpoint(self, eid):
            return {"id": "big", "provider": "custom", "base_url": "http://big/v1", "model": "big-model", "name": "big",
                    "api_key": "k"} if eid == "big" else None

    import json
    import time

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    cfg = {"kind": "rom", "platform": "gb", "arch": "sm83", "rom": {"size": 0x8000, "sha1": "a" * 40, "file": "rom.gb"},
           "compiler": {"toolchain": "gbdk-2020", "flags": ["--fsigned-char"]}}
    with net.store.tx() as db:  # a ROM project (pins are for those)
        db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                   ("rom", "ROM", "", "sm83", 0, str(tmp_path), json.dumps(cfg)))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,1,'',NULL,'',?,'')", (time.time(),))
    slug = "rom"
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "f")
    _fn(net, pid, "done", status="matched")
    with pytest.raises(ValueError, match="your own ROM projects"):
        _pin_options(net, Settings(), "api", _project(net), {"pin": "f"})
    got = _pin_options(net, Settings(), "api", slug, {"pin": "f", "lean": "several", "variants": 9, "escalate": "big"})
    assert got["pin"] == "f" and got["lean"] == "several" and got["variants"] == 6 and got["stall"] == lean.STALL
    assert got["polish"] is True and got["escalate_endpoint"] == "big" and got["escalate"]["model"] == "big-model"
    assert _pin_options(net, Settings(), "codex", slug, {"pin": "f"})["stall"] == 4  # runs, for an agent's own loop
    assert _pin_options(net, Settings(), "codex", slug, {"pin": "f", "lean": "both", "escalate": "big"})["lean"] == "both"
    assert "escalate" not in _pin_options(net, Settings(), "codex", slug, {"pin": "f", "lean": "one", "escalate": "big"})
    for kind, given, why in (("claude", {"pin": "f", "lean": "one"}, "for API helpers and Codex"),
                             ("api", {"pin": "done"}, "isn't open"),
                             ("api", {"pin": "f", "lean": "fast"}, "lean must be one of"),
                             ("api", {"pin": "f", "lean": "one", "escalate": "gone"}, "isn't saved any more")):
        with pytest.raises(ValueError, match=why):
            _pin_options(net, Settings(), kind, slug, given)
    other, _ = net.local_agent("codex-9")
    net.claim_row(other, slug, "f")
    with pytest.raises(ValueError, match="being worked on by codex-9"):
        _pin_options(net, Settings(), "api", slug, {"pin": "f"})


def test_an_agents_own_loop_steps_up_its_effort_when_stuck_and_stops(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "f")
    manager = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    s = Session(id="codex-1", kind="codex", label="Codex #1", slug=slug, agent_name="codex-1",
                options={"pin": "f", "base_effort": "low", "effort": "low"})
    efforts = []
    for stall in (0, 1, 2, 3, 4, 0):
        s.pin_stall = stall
        manager._pin_effort(s)
        efforts.append(s.options["effort"])
    assert efforts == ["low", "low", "medium", "medium", "high", "low"]  # back to the user's choice after a gain
    s.pin_stall = 4
    assert manager._pin_check(s, "f") == "no closer in 4 runs"
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=200 WHERE name='f'")
    manager._pin_after(s, "f", 500)  # the run got it from 500 to 200
    assert s.pin_stall == 0 and manager._pin_check(s, "f") == ""
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='f'")
    assert manager._pin_check(s, "f") == "f matched 🎉"



def test_a_stuck_helpers_effort_never_drops_below_the_users(tmp_path, monkeypatch):
    from decompnet.app import agents

    manager = Manager(RomNetwork(tmp_path / "d.db", tmp_path), tmp_path, "http://127.0.0.1:1/mcp")

    def steps(kind, base, config=None):
        monkeypatch.setattr(agents, "_codex_config", lambda: config or {})
        s = Session(id="s", kind=kind, label="s", slug="x", agent_name="s", options={"pin": "f", "base_effort": base})
        out = []
        for stall in (0, 2, 4):
            s.pin_stall = stall
            manager._pin_effort(s)
            out.append(s.options["effort"])
        return out

    assert steps("codex", "xhigh") == ["xhigh"] * 3  # above the ladder already: stays
    assert steps("claude", "max") == ["max"] * 3
    assert steps("codex", "", {"model_reasoning_effort": "xhigh"}) == [""] * 3  # Codex's own setting, xhigh: stays
    assert steps("codex", "", {"model_reasoning_effort": "low"}) == ["", "medium", "high"]  # up from the user's own
    assert steps("claude", "") == ["", "high", "high"]  # unknown: from medium


def test_work_on_this_starts_on_the_model_and_effort_picked_and_remembers_them(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from decompnet.app import agents
    from decompnet.app.server import build

    from .test_tiered_helpers import _net

    net, home = _net(tmp_path)
    fid = _fn(net, 1, "f")["id"]
    with net.store.tx() as db:  # code a compiler writes (li v0, 1; jr ra): zeros get set aside when a helper starts
        db.execute("UPDATE functions SET target=?, size=12, n_insns=3 WHERE id=?", (bytes.fromhex("010002240800e00300000000"), fid))
    mcp, manager, security = build(net, home / ".decomp", 6498, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6498")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    r = c.post("/api/agents/start", json={"kind": "codex", "slug": "demo", "options": {
        "pin": "f", "model": "gpt-a", "effort": "xhigh", "lean": "several", "stall": 10}}, headers=h)
    assert r.status_code == 200, r.text
    s = manager.sessions[r.json()["started"][0]]
    assert (s.options["pin"], s.options["model"], s.options["effort"], s.options["lean"]) == ("f", "gpt-a", "xhigh", "several")
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "demo", "options": {"pin": "f", "model": "opus",
                                                                                        "effort": "high"}}, headers=h)
    assert r.status_code == 200, r.text
    settings = c.get("/api/state").json()["settings"]
    assert settings["pin_helper"]["who"] == "k:claude"
    assert settings["pin_helper"]["setups"] == {"codex": {"model": "gpt-a", "effort": "xhigh"},
                                                "claude": {"model": "opus", "effort": "high"}}  # each its own
    assert (settings.get("launcher") or {}).get("model") != "gpt-a"  # the launcher's choice is its own
    assert settings["recent_models"]["codex"] == ["gpt-a"]  # a typed name comes back as a suggestion

def test_the_permuter_polishes_a_pinned_function_without_taking_it(tmp_path, monkeypatch):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "f")
    helper, _ = net.local_agent("api-1")
    net.claim_row(helper, slug, "f")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=300, best_source=? WHERE name='f'",
                   ("void f(void) {\n    u8 a = 1;\n    u8 b = 2;\n    x = a + b;\n}",))
    tried = []

    def score(agent, proj, fn, source, best, fork, st=None):
        tried.append(source)
        return 250 if len(tried) == 3 else best + 10

    monkeypatch.setattr(net.permuter, "_score", score)
    fn = net._function("f", slug)
    assert net.permuter.polish(fn, seconds=0.3) == 250
    assert net.store.one("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id")["name"] == "api-1"
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_source=NULL WHERE name='f'")
    assert net.permuter.polish(net._function("f", slug), seconds=0.3) is None  # nothing to start from


def test_a_version_without_its_declarations_gets_the_closest_ones():
    """Models often answer with the function alone, dropping what the closest version declares above it (a real
    near miss declared its two hardware registers there): those come back in front, unless the version has its own."""
    best = "__sfr __at(0x41) LCDStatus;\nvolatile __at(0xa205) u8 State;\n\nvoid f(void) {\n    State = 0;\n}\n"
    bare = "// idea: a loop\nvoid f(void) {\n    State = 1;\n}"
    got = lean.carry(bare, best, "f")
    assert got.startswith("__sfr __at(0x41) LCDStatus;\nvolatile __at(0xa205) u8 State;") and got.endswith(bare)
    own = "volatile __at(0xa205) u8 State;\nvoid f(void) { State = 2; }"
    assert lean.carry(own, best, "f") == own
    assert lean.carry(bare, None, "f") == bare


def test_register_pins_come_out_for_sdcc():
    """SDCC has no register pins; a version with them is otherwise worth judging (models write them on every
    register difference)."""
    src = 'void f(void) {\n    register u8 count asm("b");\n    register u8 *p __asm__("hl") = 0;\n    count = 1;\n}'
    assert lean.unpin(src) == "void f(void) {\n    register u8 count;\n    register u8 *p = 0;\n    count = 1;\n}"
    assert lean.unpin('void f(void) { __asm__("nop"); }') == 'void f(void) { __asm__("nop"); }'  # not a pin: refused later


def test_versions_get_directions_of_their_own_so_they_dont_repeat(tmp_path, monkeypatch):
    net, loop, model, _ = _loop(tmp_path, monkeypatch, [
        _c("x = 1;"),                        # one version, free: gains (nothing compiled before)
        _c("x = 1;"),                        # one version, free: a word-for-word repeat
        _c("x = 2;"),                        # one version: now with a direction, and told it repeated
    ] + [_c(f"y = {i};") + _c(f"z = {i};") + _c(f"w = {i};") for i in range(2)],
        {"x = 1;": 400}, method=lean.ONE)
    for _ in range(3):
        loop.round()
    first, second, third = (a["text"] for a in model.asked)
    assert "This time try" not in first and "This time try" not in second
    assert "This time try the local variables declared in a different order" in third  # (close: details)
    assert "Your last reply repeated 1 earlier version word for word" in third
    loop.method = lean.SEVERAL
    loop.round()
    loop.round()
    a, b = model.asked[3]["text"], model.asked[4]["text"]
    assert "(version 1: your own best idea; version 2: a temporary variable, or one fewer; version 3: a cast" in a
    assert "version 2: statements in another order" in b  # the next directions, not the same again


def test_a_lean_helper_works_function_after_function(tmp_path, monkeypatch):
    """Not pinned: it claims the way agents do, works each function the lean way, and moves on with a note when one
    stops getting closer; when nothing's left, it's done."""
    from decompnet.app import agents

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    _fn(net, pid, "a", insns=2)
    _fn(net, pid, "b", insns=10)
    scores = {"a = 1": 150, "b = 0": 0}

    def attempt(agent_, name, source, project):
        score = next((v for k, v in scores.items() if k in source), 180)
        net._record(net._function(name, project), agent_, score, source, 1, matched=score == 0)
        return {"text": f"score {score}", "score": score, "matched": score == 0, "rows": ROWS}

    monkeypatch.setattr(net, "attempt", attempt)
    monkeypatch.setattr(lean, "preview", lambda net_, fn, src: ROWS)
    monkeypatch.setattr(lean.Lean, "stable", lambda self, fn: ["# the target"])
    replies = iter([_c("a = 1;"), _c("a = 2;"), _c("a = 3;"), _c("b = 0;")])
    monkeypatch.setattr(agents, "_chat", lambda s, client, opts, messages, temperature, max_tokens: (next(replies), 900, "qwen"))
    manager = Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    agent, _ = net.local_agent("api-1")
    s = Session(id="api-1", kind="api", label="box #1", slug=slug, agent_name="api-1",
                options={"lean": "several", "variants": 3, "stall": 2, "base_url": "http://box/v1", "model": "qwen"})
    s.agent_id = agent.id
    manager.sessions[s.id] = s
    assert manager._run_lean(s, agent)
    fns = {r["name"]: r for r in net.store.all("SELECT * FROM functions")}
    assert fns["b"]["status"] == "matched" and fns["a"]["status"] == "open" and fns["a"]["best_score"] == 150
    released = net.store.all("SELECT detail FROM events WHERE kind='release'")
    assert [r["detail"] for r in released] == ["a"]
    assert any("from nothing to" in n["text"] for n in net.store.all("SELECT text FROM notes"))
    assert s.pin_done == "nothing left to claim here 🎉"
    assert {r["method"] for r in net.store.all("SELECT method FROM attempts")} == {"3 per call"}


def test_a_lean_start_from_the_launcher_is_checked(tmp_path):
    from decompnet.app.server import _lean_options

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    with pytest.raises(ValueError, match="for API helpers and Codex"):
        _lean_options(net, "claude", "x", {"lean": "several"})
    with pytest.raises(ValueError, match="your own ROM projects"):
        _lean_options(net, "codex", _project(net), {"lean": "several"})
