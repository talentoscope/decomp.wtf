"""GUI session survival across app restarts (the stale-GUI bug): the server keeps its secret key
across relaunches so an open browser tab (and long-lived agent sessions) stay valid, and the client
reloads once on 401 instead of freezing on stale data."""

from __future__ import annotations

from decompnet.app import cli


def test_secret_is_reused_across_relaunches(tmp_path):
    state = tmp_path / "server.json"
    # nothing on disk: a fresh key is minted
    a, b = cli._secret_for(state, None), cli._secret_for(state, None)
    assert a and b and a != b
    # a previous launch's key survives even though that app is gone (stopped or killed)
    state.write_text('{"port": 6464, "secret": "keepme", "pid": 1}')
    assert cli._secret_for(state, None) == "keepme"
    # the updater's own restart path still wins when present
    assert cli._secret_for(state, {"port": 6464, "secret": "restarting"}) == "restarting"
    # a corrupt state file falls back to a fresh key, never a crash
    state.write_text("garbage")
    assert cli._secret_for(state, None)
