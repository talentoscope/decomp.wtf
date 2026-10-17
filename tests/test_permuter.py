from types import SimpleNamespace

from decompnet.app import permute
from decompnet.app.permute import Permuter, State


def test_permuter_releases_a_function_it_cannot_rewrite(monkeypatch):
    released = []
    calls = []
    worker = Permuter(SimpleNamespace(fork_slug=lambda _: None))
    agent = SimpleNamespace(id=1)
    fn = {"id": 2, "name": "no_rewrites", "best_source": "void f(void) {}", "best_score": 1}
    st = State()

    monkeypatch.setattr(worker, "_hold", lambda *_: True)
    monkeypatch.setattr(worker, "_let_go", lambda *_: released.append(True))
    monkeypatch.setattr(worker, "_score", lambda *_: (_ for _ in ()).throw(AssertionError("no variant to score")))
    monkeypatch.setattr(permute, "mutate", lambda *_: calls.append(True) or None)

    worker._permute(agent, {"slug": "demo"}, fn, st)

    assert len(calls) == permute.NO_MUTATION_LIMIT
    assert st.tried == 1 and st.variants == 0
    assert st.current == ""
    assert released == [True]


def test_permuter_does_not_spend_a_turn_when_claim_is_taken(monkeypatch):
    worker = Permuter(SimpleNamespace(fork_slug=lambda _: None))
    monkeypatch.setattr(worker, "_hold", lambda *_: False)
    st = State()

    worker._permute(SimpleNamespace(id=1), {"slug": "demo"},
                    {"id": 2, "name": "claimed", "best_source": "", "best_score": 1}, st)

    assert worker._turns == {}
    assert st.tried == 0 and st.current == ""
