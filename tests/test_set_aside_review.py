"""A person in the loop for what's set aside: an agent sets a function aside for review with why (a few an hour at
most), and a person gives any set-aside function back to agents, which no set-aside rule takes again."""

from __future__ import annotations

import json

import pytest

from decompnet.app import tools
from decompnet.network import NetworkError

from .test_banked_layout import REST, RUNS_ON, TWO_ROUTINES, _add, net  # noqa: F401

WHY = "the split took a table of tile numbers for code: no routine starts here"


def test_an_agent_sets_a_function_aside_for_review_with_why(net):  # noqa: F811
    for k in range(5):
        _add(net, f"func_001_4{k}00", 0x4000 + k * 0x100, TWO_ROUTINES)
    agent, _ = net.local_agent("codex-1")
    net.claim_row(agent, "gbgame", "func_001_4000")
    with pytest.raises(NetworkError, match="in a sentence"):
        net.flag_for_review(agent, "gbgame", "func_001_4000", "data")
    assert net.flag_for_review(agent, "gbgame", "func_001_4000", WHY).startswith("✅")
    fn = net._function("func_001_4000", "gbgame")
    assert fn["status"] == "asm" and json.loads(fn["meta"])["review"] == WHY
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0  # its claim goes
    assert net.store.one("SELECT text FROM notes")["text"] == f"set aside for review: {WHY}"
    row = next(f for f in net.function_map("gbgame") if f["name"] == "func_001_4000")
    assert row["review"] and row["why"] == WHY and row["review_by"] == "codex-1"
    for k in (1, 2):
        net.flag_for_review(agent, "gbgame", f"func_001_4{k}00", WHY)
    with pytest.raises(NetworkError, match="release this one with notes instead"):
        net.flag_for_review(agent, "gbgame", "func_001_4300", WHY)  # a few an hour: not a way round hard ones
    with pytest.raises(NetworkError, match="isn't open"):
        net.flag_for_review(net.local_agent("claude-1")[0], "gbgame", "func_001_4000", WHY)


def test_a_person_gives_a_set_aside_function_back_and_no_rule_takes_it_again(net):  # noqa: F811
    _add(net, "func_001_4010", 0x4010, RUNS_ON)
    _add(net, "func_001_4200", 0x4200, REST)
    net.twins.set_aside_asm(net.project_row("gbgame"))  # a rule: it runs on into the next function
    assert net._function("func_001_4010", "gbgame")["status"] == "asm"
    assert net.function_detail("gbgame", "func_001_4010")["why"].startswith("runs on")
    net.give_to_agents("gbgame", "func_001_4010")
    fn = net._function("func_001_4010", "gbgame")
    assert fn["status"] == "open" and json.loads(fn["meta"]).get("for_agents") and "asm_code" not in json.loads(fn["meta"])
    net.twins.set_aside_asm(net.project_row("gbgame"))  # the pass again: a person's choice stands
    assert net._function("func_001_4010", "gbgame")["status"] == "open"
    alice = net.authenticate(net.register_agent("alice"))
    net.split_function(alice, "gbgame", "func_001_4200", "0x4202")  # (another one; the given-back one is untouched)
    assert net._function("func_001_4010", "gbgame")["status"] == "open"
    with pytest.raises(NetworkError, match="isn't set aside"):
        net.give_to_agents("gbgame", "func_001_4010")


def test_the_api_helpers_have_the_tool(net):  # noqa: F811
    _add(net, "func_001_4000", 0x4000, TWO_ROUTINES)
    agent, _ = net.local_agent("api-1")
    out = tools.call(net, agent, "gbgame", "flag_for_review", {"function": "func_001_4000", "reason": WHY})
    assert out.startswith("✅") and "flag_for_review" in tools.tool_names(fork=False)
