"""The permuter, in the background: the decomp community's way through the last few percent.

A function at 95% usually differs in register allocation or instruction order, which comes down to how the C
is written, not what it does: the order of statements and declarations, a temporary, a cast, `x += 1` for
`x++`, `register`. decomp-permuter tries thousands of such rewrites and keeps what scores better; agents doing
it by hand spend turns (and tokens) on it, and stall. Here it runs on its own, on spare CPU, on the near misses
agents left (or autopilot's drafts reached): it holds a function for a minute at a time, so an agent that
wants it gets it next, records every improvement as the function's best attempt (agents start from it), and
a match as a match. Rewrites keep the meaning of the code (see permuter.py); what matches is still judged
byte for byte, like everything else.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

from .. import narrow_params, scoring
from ..elfobj import extract_function
from ..project import compiler_settings
from ..network import NetworkError
from ..permuter import mutate

log = logging.getLogger("decomp.wtf")
NEAR = 0.06  # a near miss: at most this much of the function differs (best_score <= 6% of n_insns * 100)
SECONDS = 60  # per function per turn
MAX_TURNS = 4  # turns per function before it's left to agents


@dataclass
class State:
    state: str = "running"
    tried: int = 0  # functions permuted
    variants: int = 0
    improved: int = 0
    matched: int = 0
    failed: int = 0  # rewrites that didn't compile (or define the function): many means a lane the rewrites don't suit
    last_error: str = ""  # the last one's first line
    current: str = ""
    started_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)


class Permuter:
    def __init__(self, net):
        self.net = net
        self.jobs: dict[str, State] = {}

    def status(self, slug: str) -> dict | None:
        st = self.jobs.get(slug)
        return None if st is None else {k: v for k, v in st.__dict__.items() if k != "stop"}

    def start(self, slug: str) -> State | None:
        """Run on the project's near misses until there are none (looking again every few minutes while the
        app runs: agents make new ones)."""
        if (st := self.jobs.get(slug)) is not None and st.state == "running":
            return st
        if slug in self.net.links:  # a room member: the host's copy permutes, and its matches come back here
            return None
        st = self.jobs[slug] = State()
        threading.Thread(target=self._loop, args=(slug, st), daemon=True, name=f"permuter-{slug}").start()
        return st

    def stop(self, slug: str) -> None:
        if (st := self.jobs.get(slug)) is not None:
            st.stop.set()

    def candidates(self, proj, limit: int = 20) -> list:
        """The project's near misses, closest first, that the permuter hasn't spent its turns on at their best score
        (filtered before the limit: spent ones mustn't hide the rest)."""
        return self.net.store.all(
            "SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
            "AND best_score > 0 AND best_score <= n_insns * 100 * ? AND n_insns <= 400 "
            "AND (permuter_best IS NULL OR best_score < permuter_best OR permuter_turns < ?) "
            "AND id NOT IN (SELECT function_id FROM claims) ORDER BY CAST(best_score AS REAL) / (n_insns * 100) LIMIT ?",
            proj["id"], NEAR, MAX_TURNS, limit)

    def _spend_turn(self, fn) -> int:
        """Count a turn on `fn`, at its best score now (a better best since starts the count over). The count."""
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET permuter_turns = CASE WHEN permuter_best IS NULL OR best_score < "
                       "permuter_best THEN 1 ELSE permuter_turns + 1 END, permuter_best = best_score WHERE id=?",
                       (fn["id"],))
            return db.execute("SELECT permuter_turns FROM functions WHERE id=?", (fn["id"],)).fetchone()[0]

    def _loop(self, slug: str, st: State) -> None:
        agent, _ = self.net.local_agent("permuter")
        idle = 0
        try:
            while not st.stop.is_set():
                try:
                    proj = self.net.project_row(slug)
                except NetworkError:
                    return  # the project is gone
                todo = self.candidates(proj)
                if not todo:
                    idle += 1
                    if idle > 20:  # nothing for an hour: done until agents start again
                        return
                    st.stop.wait(180)
                    continue
                idle = 0
                for fn in todo:
                    if st.stop.is_set():
                        break
                    turn = self._spend_turn(fn)
                    try:
                        self._permute(agent, proj, fn, st, turn)
                    except Exception as e:  # one odd function must not stop the rest
                        log.info("permuter: %s: %s", fn["name"], e)
        finally:
            st.state = "stopped" if st.stop.is_set() else "done"
            st.current = ""

    def _hold(self, agent, fn) -> bool:
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                return False
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + SECONDS + 120))
        return True

    def _let_go(self, agent, fn) -> None:
        with self.net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _permute(self, agent, proj, fn, st: State, turn: int = 1) -> None:
        if not self._hold(agent, fn):
            return
        st.current, st.tried = fn["name"], st.tried + 1
        try:
            self._climb(agent, proj, fn, st, SECONDS, random.Random(f"{fn['id']}-{turn}"), st.stop)
        finally:
            self._let_go(agent, fn)

    def polish(self, fn, seconds: float = 20, stop: threading.Event | None = None) -> int | None:
        """A turn on a function a helper is pinned to, between its calls (the helper keeps its claim): from the
        closest source at any percentage, not just near misses. The best score after, or None (nothing to start
        from). Improvements are recorded as the permuter's, like the background pass's."""
        if not fn["best_source"] or fn["best_score"] is None or fn["best_score"] <= 0:
            return None
        agent, _ = self.net.local_agent("permuter")
        proj = self.net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        st = State()  # its own counts: the background pass's stay as they are
        return self._climb(agent, proj, fn, st, seconds, random.Random(f"{fn['id']}-polish-{time.time()}"), stop or st.stop)

    def _climb(self, agent, proj, fn, st: State, seconds: float, rng: random.Random, stop: threading.Event) -> int:
        """Rewrites of the function's best source for `seconds`, mostly downhill: returns the best score reached."""
        fork = self.net.fork_slug(proj["slug"])
        best_src = cur_src = fn["best_source"]
        best = cur = fn["best_score"]
        deadline = time.monotonic() + seconds
        pending = []
        if not fork and not stop.is_set() and time.monotonic() < deadline:
            cfg = self.net.cfg(proj)
            if cfg.get("platform") == "psx" and narrow_params.knr_variant(best_src, fn["name"]):
                profile = compiler_settings(cfg, fn["unit"])
                if profile.get("toolchain", "").endswith(("-psx", "-cdk")):
                    ctx = self.net._context(proj["id"], fn["unit"])
                    baseline = self.net._compile_function(proj, fn, ctx["header"], best_src)
                    obj = extract_function(baseline.obj, fn["name"]) if baseline.ok else None
                    if obj:
                        pending = narrow_params.targeted(best_src, fn["name"], profile["toolchain"], fn["target"] or b"", obj.code)
        empty = 0
        while time.monotonic() < deadline and best > 0 and not stop.is_set():
            cand = pending.pop(0) if pending else mutate(cur_src if rng.random() < 0.85 else best_src, rng)
            if cand is None:
                empty += 1
                if empty > 200:  # nothing in it to rewrite
                    break
                continue
            empty = 0
            st.variants += 1
            score = self._score(agent, proj, fn, cand, best, fork, st)
            if score is None:
                continue
            if score < best:
                best, best_src = score, cand
                st.improved += 1
                if score == 0:
                    st.matched += 1
                    log.info("permuter: %s matched", fn["name"])
                    break
            if score <= cur or rng.random() < 0.02:  # mostly downhill, now and then sideways off a plateau
                cur, cur_src = score, cand
        return best

    def _score(self, agent, proj, fn, source: str, best: int, fork: str | None, st: State | None = None) -> int | None:
        """A variant's score; recorded (as the function's best attempt, or its match) only when it beats `best`.
        A variant that doesn't compile is counted in `st` with its error: the permuter's rewrites are C a compiler
        may not take (SDCC's dialect), and that should show, not pass in silence."""
        if fork:
            res = self.net.forks.attempt(agent, fork, fn["name"], source, keep=False, quiet=best)
            return res.get("score")
        ctx = self.net._context(proj["id"], fn["unit"])
        result = self.net._compile_function(proj, fn, ctx["header"], source)
        if not result.ok:
            if st is not None:
                st.failed += 1
                st.last_error = next((ln.strip() for ln in (result.log or "").splitlines() if ln.strip()), "")[:200]
            return None
        cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
        if cand is None:
            if st is not None:
                st.failed += 1
                st.last_error = f"the build has no {fn['name']}"
            return None
        d = scoring.diff(self.net._target_obj(fn, proj), cand)
        if verdict is not None:
            d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
        if d.score < best:
            self.net._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match, via="permuter")
        return 0 if d.bytes_match else max(1, d.score)
