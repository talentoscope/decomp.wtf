"""SDCC's `__naked` leaves out the function's return, so it matched pieces of hand-written routines that no C function
is. The source check refuses it now, and what it matched before is open again, once, with the best attempt that
didn't use it."""

from decompnet.app.engine import RomNetwork

from .test_progress import _fn
from .test_room_identity import _project

NAKED = "void f(void) __naked { x = 0; }"


def _match(net, fid, agent, source, score=0):
    with net.store.tx() as db:
        db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,context_version,source,created_at) "
                   "VALUES(?,?,?,1,1,?,1)", (fid, agent.id, score, source))
        if score == 0:
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=1, "
                       "best_score=0, best_source=? WHERE id=?", (source, agent.id, source, fid))


def test_what_naked_matched_is_open_again_once(tmp_path):
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = _project(net)
    pid = net.project_row(slug)["id"]
    agent, _ = net.local_agent("codex-1")
    piece, whole, real, said = (_fn(net, pid, n)["id"] for n in ("piece", "whole", "real", "said"))
    _match(net, piece, agent, NAKED)                               # only __naked got it
    _match(net, whole, agent, "void f(void) { x = 1; }", score=120)  # a real try first
    _match(net, whole, agent, NAKED)
    _match(net, real, agent, NAKED)
    _match(net, real, agent, "void f(void) { x = 0; }")            # C matched it too
    _match(net, said, agent, "void f(void) { /* not __naked */ x = 0; }")
    with net.store.tx() as db:  # a database from before the check
        db.execute("DELETE FROM settings WHERE key='naked_reopened'")
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    fn = {f["id"]: f for f in net.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
    assert fn[piece]["status"] == "open" and fn[piece]["best_score"] is None and fn[piece]["matched_source"] is None
    assert fn[whole]["status"] == "open" and fn[whole]["best_score"] == 120 and "x = 1" in fn[whole]["best_source"]
    assert fn[real]["status"] == "matched" and "__naked" not in fn[real]["matched_source"]
    assert fn[said]["status"] == "matched"
    events = net.store.all("SELECT detail FROM events WHERE kind='reopen'")
    assert len(events) == 1 and events[0]["detail"].startswith("2 matches made with __naked")
    _match(net, piece, agent, NAKED)  # (the check refuses this now; a match made by hand stays as it is)
    RomNetwork(tmp_path / "d.db", tmp_path)
    assert net.store.one("SELECT status FROM functions WHERE id=?", piece)["status"] == "matched"
