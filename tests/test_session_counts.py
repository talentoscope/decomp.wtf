"""An agent session's card counts what that session did, not everything its name ever did: names such as claude-1
come back after a restart."""

import time

from decompnet.app.engine import RomNetwork

from .test_rom import built  # noqa: F401

ABS = "s32 func_800007F4(s32 v) { return v < 0 ? -v : v; }"


def test_a_session_counts_only_its_own_work(built, tmp_path):  # noqa: F811
    net = RomNetwork(tmp_path / "d.db", tmp_path)
    slug = net.import_rom(built, confirmed=True, log=lambda *_: None)
    agent, _ = net.local_agent("claude-1")
    other, _ = net.local_agent("claude-2")
    started = time.time() - 1
    assert net.session_counts(slug, "claude-1", started) == {"compiles": 0, "matched": 0}
    net.claim(agent, slug, "func_800007F4")
    assert net.compile_and_diff(agent, "func_800007F4", ABS, slug).startswith("✅ MATCH")
    assert net.session_counts(slug, "claude-1", started) == {"compiles": 1, "matched": 1}
    assert net.session_counts(slug, "claude-2", started) == {"compiles": 0, "matched": 0}  # another agent's work
    later = time.time() + 1  # the app restarted and a new claude-1 began
    assert net.session_counts(slug, "claude-1", later) == {"compiles": 0, "matched": 0}
    assert net.live(slug)["overview"] is not None
