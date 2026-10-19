"""A member of a room that approves its members waits to be let in before asking for the project's files."""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from decompnet.app import collab
from decompnet.network import NetworkError

WAITING = "waiting for the host to let you in"


def host(answers: list[str | None]) -> TestClient:
    """A host whose /collab/whoami answers with each error in turn (None: the member is in)."""
    async def whoami(request):
        answer = answers.pop(0)
        return JSONResponse({"name": "alex"} if answer is None else {"error": answer}, status_code=200 if answer is None else 400)
    return TestClient(Starlette(routes=[Route("/collab/whoami", whoami)]), base_url="https://room.example")


def wait(answers, said, **kw):
    collab._wait_to_be_let_in(host(answers), "https://room.example", "room", {}, said.append, every=0.01, **kw)


def test_the_member_waits_until_the_host_lets_them_in():
    said, answers = [], [WAITING, WAITING, None]
    wait(answers, said)
    assert not answers
    assert said == [WAITING, "the host let you in"]


def test_a_declined_member_is_told_so():
    with pytest.raises(NetworkError, match="didn't let you in"):
        wait([WAITING, "invalid API token"], [])


def test_other_errors_are_passed_on():
    with pytest.raises(NetworkError, match="this room is closed"):
        wait(["this room is closed"], [])


def test_the_wait_ends_when_nobody_lets_the_member_in():
    with pytest.raises(NetworkError, match="hasn't let you in yet"):
        wait([WAITING] * 100, [], patience=0.05)
