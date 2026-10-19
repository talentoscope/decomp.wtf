"""Helper agents: each CLI's output read into the live feed (Antigravity CLI, opencode errors), prompts sent on
stdin, `decomp connect`, and the compact context agents get (assembly, diffs)."""

import json
import types

import pytest

from decompnet import scoring
from decompnet.app import agents, connect


def test_antigravity_stream_json_is_read():
    state: dict = {}
    lines = [
        {"event": "init", "conversation_id": "c", "init": {"cwd": "/w", "tools": ["call_mcp_tool"]}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response",
                                                 "text_delta": "Claiming "}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response",
                                                 "text_delta": "the next one."}},
        {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                 "text_delta": "\n"}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool",
                                                 "tool_name": "call_mcp_tool", "tool_info": {"name": "call_mcp_tool",
                                                 "parameters": {"ServerName": "decomp", "ToolName": "claim_function",
                                                                "Arguments": {"strategy": "easiest"}}}}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool",
                                                 "tool_name": "call_mcp_tool", "tool_info": {"name": "call_mcp_tool",
                                                 "parameters": {"ServerName": "decomp", "ToolName": "claim_function",
                                                                "Arguments": {"strategy": "easiest"}}}}},
        {"event": "result", "result": {"status": "SUCCESS", "num_turns": 3, "usage": {"total_tokens": 1200}}},
    ]
    out = [item for line in lines for item in agents.parse_line("antigravity", json.dumps(line), state)]
    assert out == [("text", "Claiming the next one."), ("tool", "claim_function(easiest)"),
                   ("usage", json.dumps({"tokens": 1200})), ("info", "session finished (3 turns)")]


def test_antigravity_refusing_decomps_tools_says_how_to_allow_them():
    line = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "",
                                                     "denied_actions": [{"action": "mcp", "display_name": "CallMcpTool"}]}})
    errors = [t for k, t in agents.parse_line("antigravity", line, {}) if k == "error"]
    assert errors and errors[0].startswith("Antigravity refused") and agents.AGY_ALLOW in errors[0]


def test_opencode_errors_say_what_failed():
    """Issue #3: opencode's error event nests the message under data; the feed showed "None"."""
    line = json.dumps({"type": "error", "sessionID": "s", "error": {"name": "ProviderAuthError",
                                                                    "data": {"message": "no key for deepseek"}}})
    assert agents.parse_line("opencode", line) == [("error", "ProviderAuthError: no key for deepseek")]
    generic = json.dumps({"type": "error", "error": {"name": "UnknownError", "data": {"message": "Unexpected server error."}}})
    assert agents.parse_line("opencode", generic) == []  # a placeholder: the log line on stderr says what failed
    assert agents.parse_line("codex", json.dumps({"type": "turn.failed", "error": {"message": "quota"}})) == [("error", "quota")]
    log = ('timestamp=2026-10-14T14:25:35.946Z level=ERROR run=89d82ac2 message=failed ref=err_ab91f09e '
           'error="ProviderModelNotFoundError: Model not found: nosuch/model." cause="ProviderModelNotFoundError: x\\n at y"')
    assert agents.clean_stderr("opencode", log) == "ProviderModelNotFoundError: Model not found: nosuch/model."
    assert agents.clean_stderr("opencode", "timestamp=1 level=WARN message=slow") == ""
    agy = 'AGY_ERROR: {"message": "quota exhausted", "code": 429}'
    assert agents.clean_stderr("antigravity", agy) == "quota exhausted"


def _manager(tmp_path, monkeypatch, kind):
    (tmp_path / "bin").mkdir(exist_ok=True)
    exe = tmp_path / "bin" / {"claude": "claude", "codex": "codex", "antigravity": "agy", "opencode": "opencode"}[kind]
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    m = agents.Manager.__new__(agents.Manager)
    m.mcp_url = "http://127.0.0.1:6464/mcp"
    m.prompt = lambda s: "line one\nline two with \"quotes\" & <ampersands>"
    return m


def test_prompts_go_in_on_stdin_never_the_command_line(tmp_path, monkeypatch):
    """On Windows, npm installs agents as .cmd scripts: cmd.exe cuts a multi-line argument at its first line break
    and caps the command line at 8191 characters."""
    for kind in ("claude", "codex", "antigravity", "opencode"):
        m = _manager(tmp_path, monkeypatch, kind)
        s = agents.Session(f"{kind}-1", kind, kind, "demo", f"{kind}-1", {"model": "some-model"})
        work = tmp_path / kind
        work.mkdir()
        cmd, env, stdin = m._command(s, "tok", work)
        assert not any("line one" in a for a in cmd), (kind, cmd)
        assert "line one" in stdin and "some-model" in cmd
        assert env["DECOMP_SESSION_TOKEN"] == "tok"  # a `decomp mcp` the agent starts works as this session
    # Antigravity: one stream-json user message, the server in the workspace's own MCP config
    m = _manager(tmp_path, monkeypatch, "antigravity")
    s = agents.Session("antigravity-2", "antigravity", "a", "demo", "antigravity-2", {})
    (tmp_path / "agy2").mkdir()
    cmd, _, stdin = m._command(s, "tok", tmp_path / "agy2")
    assert json.loads(stdin) == {"event": "user", "message": {"content": "line one\nline two with \"quotes\" & <ampersands>"}}
    assert cmd[1:5] == ["--output-format", "stream-json", "--input-format", "stream-json"]
    cfg = json.loads((tmp_path / "agy2" / ".agents" / "mcp_config.json").read_text())
    assert cfg["mcpServers"]["decomp"] == {"serverUrl": "http://127.0.0.1:6464/mcp",
                                           "headers": {"Authorization": "Bearer tok"}}


def test_opencode_free_models_keep_their_tools_but_never_get_to_use_them(tmp_path, monkeypatch):
    """OpenCode Zen's free models refuse requests whose tools were taken out ("deny"); "ask" keeps them listed and
    `opencode run` rejects every ask. Models of other providers get "deny"."""
    monkeypatch.setattr(agents, "_config_model", lambda kind: None)
    for model, rule in (("opencode/big-pickle", "ask"), ("", "ask"), ("deepseek/deepseek-chat", "deny")):
        m = _manager(tmp_path, monkeypatch, "opencode")
        s = agents.Session("opencode-1", "opencode", "o", "demo", "opencode-1", {"model": model})
        work = tmp_path / f"oc-{rule}-{model.replace('/', '_')}"
        work.mkdir()
        m._command(s, "tok", work)
        assert json.loads((work / "opencode.json").read_text())["permission"] == {"edit": rule, "bash": rule, "webfetch": rule}


def test_gemini_cli_helpers_carry_on_as_antigravity():
    assert agents.kind_id("gemini") == "antigravity" and agents.kind_id("agy") == "antigravity"
    assert "gemini" not in agents.KINDS and agents.KINDS["antigravity"].command == "agy"


def test_shutdown_removes_helpers_and_clears_resume_state():
    manager = agents.Manager.__new__(agents.Manager)
    sessions = [agents.Session("a", "api", "API", "demo", "a", {}),
                agents.Session("b", "claude", "Claude", "demo", "b", {})]
    manager.sessions = {s.id: s for s in sessions}
    saved = []
    manager.persist = lambda running: saved.append(running)
    released = []
    manager.net = types.SimpleNamespace(permuter=types.SimpleNamespace(jobs={"demo": 1}, stop=released.append),
                                        release_local_claims=lambda: released.append("claims"))

    manager.shutdown()

    assert manager.sessions == {}
    assert all(s.stop.is_set() and s.state == "stopped" for s in sessions)
    assert saved == [[]] and released == ["demo", "claims"]


def test_release_local_claims_keeps_other_agents(tmp_path):
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "t.db", tmp_path)
    helper, _ = net.local_agent("api-1")
    other = net.authenticate(net.register_agent("someone", "them", origin="web"))
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(1,?,0,9e9)", (helper.id,))
        db.execute("INSERT INTO claims VALUES(2,?,0,9e9)", (other.id,))

    net.release_local_claims()

    assert [r["function_id"] for r in net.store.all("SELECT function_id FROM claims")] == [2]


def test_connect_antigravity(tmp_path, monkeypatch):
    monkeypatch.setattr(connect, "_which", lambda cmd: f"/bin/{cmd}")
    monkeypatch.setattr(connect, "bridge_command", lambda: ["/usr/bin/python3", "-m", "decompnet.app.cli", "mcp"])
    cmd = connect.commands("antigravity")
    # agy wants flags before the name, and `--` before a command whose arguments start with "-"
    assert cmd == ["/bin/agy", "mcp", "add", "-e", "DECOMP_AGENT_NAME=antigravity", "decomp", "--",
                   "/usr/bin/python3", "-m", "decompnet.app.cli", "mcp"]


def test_assembly_for_agents_is_compact():
    asm = ("glabel func_80013C60\n/* 4460 80013C60 27BDFF88 */  addiu      $sp, $sp, -0x78\n"
           "/* 4464 80013C64 AFB10054 */  sw         $s1, 0x54($sp)\n.L80013CC4:\n"
           "/* 44C8 80013CC8 00000000 */  nop\n")
    assert scoring.compact_asm(asm) == ("glabel func_80013C60\n  addiu $sp, $sp, -0x78\n  sw $s1, 0x54($sp)\n"
                                        ".L80013CC4:\n  nop")
    assert scoring.compact_asm("\tpush {r4, lr}\n") == "\tpush {r4, lr}"  # other listings stay as they are


def test_diffs_sum_up_and_shorten_long_runs():
    rows = [(" ", "addiu $sp, $sp, -8", "addiu $sp, $sp, -8"), ("r", "move $s0, $a0", "move $s1, $a0")]
    rows += [("<", f"lw $v0, {i}($s0)", "") for i in range(300)]
    rows += [(" ", "jr $ra", "jr $ra")]
    text = scoring.render_rows(rows)
    assert text.splitlines()[0] == "300 missing, 1 regalloc"
    assert "... 297 more missing (target only) ..." in text and "lw $v0, 299($s0)" in text
    assert len(text.splitlines()) < 15


def test_helpers_and_the_agent_you_talk_to_never_share_an_identity(tmp_path):
    """8 Antigravity helpers showed as 15 agents: the helpers reached decomp.wtf through the user's own `decomp mcp`,
    which signed each in as a new agent of the user's, named like the helpers (antigravity-9...). Chat identities
    are numbered differently now, and a helper never counts as the agent the user talks to."""
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "app.db", tmp_path)
    a, _ = net.chat_agent("antigravity")
    b, _ = net.chat_agent("antigravity")  # a second chat session at the same time
    assert (a.name, b.name) == ("antigravity", "antigravity_2")
    net.register_agent("antigravity-2", "you", origin="chat")  # a chat identity from before, named like a helper
    helper, _ = net.local_agent("antigravity-2")
    assert not net.is_chat(helper) and net.is_chat(a)
    assert net.local_agent("autopilot")[0].name == "autopilot"


def test_antigravity_helpers_say_when_it_isnt_connected(tmp_path, monkeypatch):
    from decompnet.app import connect

    m = _manager(tmp_path, monkeypatch, "antigravity")
    monkeypatch.setattr(connect, "status", lambda kind: False)
    s = agents.Session("antigravity-1", "antigravity", "a", "demo", "antigravity-1", {})
    s.runs = 1
    with pytest.raises(agents.Fatal, match="decomp connect antigravity"):
        m._run_cli(s, "tok", tmp_path)


CODEX_CATALOG = {"models": [
    {"slug": "gpt-b", "display_name": "GPT-B", "description": "second", "visibility": "list", "priority": 2,
     "default_reasoning_level": "medium", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
    {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1},
    {"slug": "gpt-a", "display_name": "GPT-A", "description": "first", "visibility": "list", "priority": 1,
     "default_reasoning_level": "low", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]}]}


def _fake_codex(tmp_path, catalog=CODEX_CATALOG, bundled=None):
    """A `codex` whose `debug models` prints the catalog (`--bundled`: the other one, or a failure)."""
    exe = tmp_path / "codex"
    data = tmp_path / "catalog.json"
    data.write_text(json.dumps(catalog))
    bundled_data = tmp_path / "bundled.json"
    bundled_data.write_text(json.dumps(bundled))
    exe.write_text('#!/bin/sh\n[ "$1 $2" = "debug models" ] || exit 2\n'
                   + (f'if [ "$3" = "--bundled" ]; then cat {bundled_data}; exit 0; fi\n' if bundled is not None else
                      'if [ "$3" = "--bundled" ]; then exit 1; fi\n')
                   + (f"cat {data}\n" if catalog is not None else "exit 1\n"))
    exe.chmod(0o755)
    return str(exe)


def test_codex_models_are_the_ones_its_own_picker_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_models", {})
    got = agents.models("codex", _fake_codex(tmp_path))
    assert [m["id"] for m in got] == ["gpt-a", "gpt-b"]  # hidden ones left out, best first
    assert got[0] == {"id": "gpt-a", "name": "GPT-A", "note": "first", "efforts": ["low", "high"], "effort": "low"}
    assert got[1]["efforts"] == ["low", "medium"] and got[1]["effort"] == "medium"


def test_codex_models_fall_back_to_the_catalog_bundled_with_it(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_models", {})
    assert [m["id"] for m in agents.models("codex", _fake_codex(tmp_path, catalog=None, bundled=CODEX_CATALOG))] == ["gpt-a", "gpt-b"]
    monkeypatch.setattr(agents, "_models", {})
    assert agents.models("codex", _fake_codex(tmp_path, catalog=None)) == []  # nothing to offer: models can still be typed


def test_codex_effort_is_passed_on_only_when_chosen(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_config", lambda: {})
    for effort, expected in (("high", ['model_reasoning_effort="high"']), ("", []), ('x"; rm -rf /', [])):
        m = _manager(tmp_path, monkeypatch, "codex")
        s = agents.Session("codex-1", "codex", "c", "demo", "codex-1", {"model": "gpt-a", "effort": effort})
        work = tmp_path / f"cx-{len(effort)}"
        work.mkdir()
        cmd, _, _ = m._command(s, "tok", work)
        assert [a for a in cmd if a.startswith("model_reasoning_effort")] == expected, effort
        assert cmd[cmd.index("-m") + 1] == "gpt-a"


def test_codex_default_effort_is_the_users_own_setting(monkeypatch):
    monkeypatch.setattr(agents, "_codex_config", lambda: {"model": "gpt-a", "model_reasoning_effort": "xhigh"})
    assert agents._config_effort("codex") == "xhigh" and agents._config_model("codex") == "gpt-a"
    assert agents._config_effort("claude") is None
    monkeypatch.setattr(agents, "_codex_config", lambda: {})
    assert agents._config_effort("codex") is None


def test_opencode_own_install_beats_a_stale_copy_on_path(tmp_path, monkeypatch):
    def exe(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return str(p)
    home, brew = tmp_path / "home", tmp_path / "brew"
    home.mkdir()
    monkeypatch.setattr(agents.Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(brew))
    on_path = exe(brew / "opencode")
    assert agents._which("opencode") == on_path  # only the PATH copy: still found
    own = exe(home / ".opencode" / "bin" / "opencode")
    assert agents._which("opencode") == own


def _opencode(tmp_path, name, run_help, version="1.18.2"):
    exe = tmp_path / name
    exe.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo {version}; exit 0; fi\n'
                   f"if [ \"$1 $2\" = \"run --help\" ]; then cat <<'EOF'\n{run_help}\nEOF\nfi\n")
    exe.chmod(0o755)
    return str(exe)


def test_opencode_log_level_is_spelled_the_way_that_opencode_accepts(tmp_path, monkeypatch):
    """1.x lists DEBUG/INFO/WARN/ERROR and prints its help instead of running when given "error"; 2.x refuses "ERROR"."""
    monkeypatch.setattr(agents, "_log_levels", {})
    upper = '      --log-level    log level    [string] [choices: "DEBUG", "INFO", "WARN", "ERROR"]'
    lower = '      --log-level    log level    [string] [choices: "debug", "info", "warn", "error"]'
    assert agents.opencode_error_level(_opencode(tmp_path, "oc1", upper)) == "ERROR"
    assert agents.opencode_error_level(_opencode(tmp_path, "oc2", lower, "2.0.3")) == "error"
    assert agents.opencode_error_level(_opencode(tmp_path, "oc3", "usage: opencode run", "2.1.0")) == "error"  # by version
    assert agents.opencode_error_level(_opencode(tmp_path, "oc4", "usage: opencode run", "1.18.2")) == "ERROR"
    assert agents.opencode_error_level(str(tmp_path / "missing")) == "ERROR"  # can't ask: the long-standing spelling
    m = _manager(tmp_path, monkeypatch, "opencode")
    monkeypatch.setattr(agents, "opencode_error_level", lambda exe: "error")
    s = agents.Session("opencode-1", "opencode", "o", "demo", "opencode-1", {})
    (tmp_path / "oc-work").mkdir()
    cmd, _, _ = m._command(s, "tok", tmp_path / "oc-work")
    assert cmd[cmd.index("--log-level") + 1] == "error"


def test_stopping_a_helper_gives_its_function_back_at_once(tmp_path):
    """An API helper waiting on its model kept its claim until the reply came, and showed as a connected agent."""
    from decompnet.app.engine import RomNetwork

    net = RomNetwork(tmp_path / "d.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1,'game','game',?,?)",
                   (str(tmp_path), json.dumps({"kind": "rom"})))
        db.execute("INSERT INTO functions(project_id, name, size, n_insns) VALUES(1,'f',4,1)")
    m = agents.Manager(net, tmp_path, "http://127.0.0.1:1/mcp")
    agent, _ = net.local_agent("api-1")
    net.claim_row(agent, "game", "f")
    s = agents.Session("api-1", "api", "API #1", "game", "api-1", {})
    s.agent_id, s.state = agent.id, "working"
    m.sessions[s.id] = s
    m.stop(s.id)
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
    assert net._function("f", "game")["abandons"] == 0  # not the function's fault
