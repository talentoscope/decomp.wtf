"""A room's identity apart from its address (identity.py): its key, the note of where it is now that a room shared
through a quick tunnel keeps in the host's gist, a member following the room when the tunnel's address changes, and
the keeper that keeps the tunnel up (collab.Keeper)."""

import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from decompnet.app import collab, identity
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


TOKEN = "ghp_" + "x" * 36


def github() -> tuple[TestClient, dict]:
    """GitHub's gist API, as the app uses it: secret gists made and changed with the host's token, read by anyone."""
    gists: dict[str, dict] = {}
    made = []

    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {TOKEN}"

    async def user(request: Request):
        if not authorized(request):
            return JSONResponse({"message": "Bad credentials"}, status_code=401)
        return JSONResponse({"login": "host-account"}, headers={"x-oauth-scopes": "gist"})

    async def make(request: Request):
        if not authorized(request):
            return JSONResponse({"message": "Requires authentication"}, status_code=401)
        body = await request.json()
        made.append(1)
        gid = f"{len(made):032x}"
        gists[gid] = {"id": gid, "public": body["public"], "files": body["files"]}
        return JSONResponse(gists[gid], status_code=201)

    async def gist(request: Request):
        gid = request.path_params["gid"]
        if gid not in gists:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        if request.method == "PATCH":
            if not authorized(request):
                return JSONResponse({"message": "Not Found"}, status_code=404)
            gists[gid]["files"].update((await request.json())["files"])
        return JSONResponse(gists[gid])

    app = Starlette(routes=[Route("/user", user), Route("/gists", make, methods=["POST"]),
                            Route("/gists/{gid}", gist, methods=["GET", "PATCH"])])
    return TestClient(app, base_url=identity.GITHUB), gists


def _note(gists: dict, gid: str) -> dict:
    import json

    return json.loads(gists[gid]["files"][identity.NOTE]["content"])


def _project(net: RomNetwork, slug: str = "game") -> str:
    """A project to share, without a game or compilers (the room's identity doesn't need them)."""
    root = net.workspace / slug
    root.mkdir(parents=True)
    net.forks._register(slug, "Game", "fork", root, {"kind": "fork", "version": "X", "fork_kind": "objdiff", "ready": True}, "x")
    return slug


@pytest.fixture
def host(tmp_path):
    net = RomNetwork(tmp_path / "h.db", tmp_path / "h")
    slug = _project(net)
    room = collab.Room(net, slug)
    room.open_invite(mode="tunnel")
    net.rooms[slug] = room
    return net, slug, room


def test_a_key_signs_and_only_its_own_signatures_check():
    secret, other = identity.new_secret(), identity.new_secret()
    key = identity.public_key(secret)
    assert identity.KEY.match(key) and key != identity.public_key(other)
    sig = identity.sign(secret, "hello")
    assert identity.verify(key, "hello", sig)
    assert not identity.verify(key, "hello!", sig)
    assert not identity.verify(key, "hello", identity.sign(other, "hello"))
    assert not identity.verify(key, "hello", "not a signature")


def test_a_rooms_key_stays_with_it_and_is_in_its_links(host):
    net, slug, room = host
    key = room.key
    assert collab.Room(net, slug).key == key  # kept with the project: the same room after a restart
    link = room.link("https://abc.trycloudflare.com/")
    assert link.endswith(f"#room={slug}&key={key}")
    assert collab.parse_invite(link)[0] == "https://abc.trycloudflare.com"
    assert collab._fragment(link) == {"room": slug, "key": key}
    room.configure(visibility="public")
    assert "/join/open#" in room.link("https://abc.trycloudflare.com")
    assert key not in str(net.project_row(slug)["config"]).replace(room.secret, "")  # only the secret is stored


def test_a_room_shows_it_holds_its_key_at_its_address(host):
    net, slug, room = host
    room.url = "https://abc.trycloudflare.com"
    client = TestClient(collab.host_app(net, net.rooms, tunnel=True), base_url=room.url)
    assert identity.identify(client, room.url, slug) == room.key
    # someone else's address in front of the same room (a proxy reading the members' traffic): the host signed its
    # own address, not that one
    with pytest.raises(NetworkError, match="couldn't show"):
        identity.identify(client, "https://proxy.trycloudflare.com", slug)


def test_a_room_says_where_it_is_in_a_secret_gist(host):
    net, slug, room = host
    gh, gists = github()
    gid, note = identity.publish(room.secret, "https://abc.trycloudflare.com", TOKEN, "", gh)
    assert gists[gid]["public"] is False and _note(gists, gid)["url"] == "https://abc.trycloudflare.com"
    assert identity.lookup(room.key, gid, gh) == "https://abc.trycloudflare.com"  # read with no token
    assert identity.publish(room.secret, "https://def.trycloudflare.com", TOKEN, gid, gh)[0] == gid  # the same gist
    assert identity.lookup(room.key, gid, gh) == "https://def.trycloudflare.com"
    assert identity.lookup(room.key, gid, gh, now=note["expires"] + identity.POINTER_LIFE) is None  # expired
    del gists[gid]  # deleted on GitHub: a new one
    assert identity.publish(room.secret, "https://ghi.trycloudflare.com", TOKEN, gid, gh)[0] != gid


def test_what_a_gist_says_counts_only_when_the_room_signed_it(host):
    net, slug, room = host
    gh, gists = github()
    gid, note = identity.publish(room.secret, "https://abc.trycloudflare.com", TOKEN, "", gh)
    import json
    gists[gid]["files"][identity.NOTE]["content"] = json.dumps({**note, "url": "https://evil.trycloudflare.com"})
    assert identity.lookup(room.key, gid, gh) is None
    other = identity.new_secret()  # another room's note, in a gist this room's members were told about
    gid2, _ = identity.publish(other, "https://evil.trycloudflare.com", TOKEN, "", gh)
    assert identity.lookup(room.key, gid2, gh) is None
    assert identity.lookup(room.key, "not-a-gist", gh) is None


def test_only_a_token_that_can_make_gists_connects(host):
    gh, gists = github()
    assert identity.github_login(TOKEN, gh) == "host-account"
    with pytest.raises(ValueError, match="didn't take that token"):
        identity.github_login("ghp_wrong", gh)

    async def no_gists(request):
        return JSONResponse({"login": "someone"}, headers={"x-oauth-scopes": "repo, read:user"})
    narrow = TestClient(Starlette(routes=[Route("/user", no_gists)]), base_url=identity.GITHUB)
    with pytest.raises(ValueError, match="gist scope"):
        identity.github_login(TOKEN, narrow)


def test_a_gist_github_turns_down_is_an_error(host):
    net, slug, room = host
    gh, gists = github()
    with pytest.raises(ValueError, match="authentication"):
        identity.publish(room.secret, "https://abc.trycloudflare.com", "ghp_wrong", "", gh)


class FakeTunnel:
    """A quick tunnel that opens at a new address each time."""
    opened = 0

    def __init__(self, port):
        self.port, self.url, self.up, self.reach, self.started = port, None, False, True, 0.0

    def start(self, log=print):
        FakeTunnel.opened += 1
        self.url, self.up, self.started = f"https://tunnel-{FakeTunnel.opened}.trycloudflare.com", True, time.time()
        return self.url

    def alive(self):
        return self.up

    def reachable(self, room, http=None):
        return self.reach

    def stop(self):
        self.up = False


def _keeper(net, token=TOKEN):
    gh, gists = github()
    share = {"servers": {("127.0.0.1", None): (None, 4321)}, "tunnel": None, "links": {}}
    keeper = collab.Keeper(net, share, log=lambda *_: None, github=gh, token=lambda: token, tunnel=FakeTunnel)
    return keeper, share, gh, gists


def test_the_keeper_opens_the_tunnel_and_the_room_says_where_it_is(host):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    url = share["tunnel"].url
    assert room.url == url and share["links"][slug] == room.link(url)
    assert identity.lookup(room.key, room.gist, gh) == url  # in the host's gist
    assert any("quick tunnel opened" in e["detail"] for e in net.live(slug)["events"])


def test_the_keeper_opens_a_new_tunnel_when_the_old_one_stops(host):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    first = share["tunnel"].url
    share["tunnel"].up = False  # cloudflared stopped
    keeper.tick()
    assert share["tunnel"].url != first and room.url == share["tunnel"].url
    assert identity.lookup(room.key, room.gist, gh) == room.url  # said again, at the new address


def test_the_keeper_opens_a_new_tunnel_when_its_address_stops_answering(host):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    now = time.time()
    keeper.tick(now)
    first = share["tunnel"]
    first.reach = False  # cloudflared runs, but its one connection dropped
    keeper.tick(now + 30)  # checked every few minutes, not every tick
    assert keeper.fails == 0
    for i in range(keeper.FAILS):  # then again soon after each failed check
        keeper.tick(now + keeper.CHECK_EVERY + i * keeper.RECHECK)
    assert share["tunnel"] is first and keeper.fails == keeper.FAILS
    keeper.tick(now + keeper.CHECK_EVERY + keeper.FAILS * keeper.RECHECK)
    assert share["tunnel"] is not first and share["tunnel"].alive() and keeper.fails == 0


def test_the_keeper_waits_longer_each_time_a_tunnel_doesnt_open(host):
    net, slug, room = host

    class Offline(FakeTunnel):
        def start(self, log=print):
            raise RuntimeError("couldn't open a tunnel (is the internet reachable?)")

    keeper, share, gh, gists = _keeper(net)
    keeper.tunnel = Offline
    now = time.time()
    keeper.tick(now)
    assert share["tunnel"] is None and keeper.retry_at == now + 30
    keeper.tick(now + 10)  # too soon
    assert keeper.retry_at == now + 30
    keeper.tick(now + 31)
    assert keeper.retry_at == now + 31 + 60
    keeper.tunnel = FakeTunnel  # back online
    keeper.tick(now + 200)
    assert share["tunnel"].alive() and room.url == share["tunnel"].url


def test_a_room_publishes_again_only_when_its_note_gets_old(host):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    now = time.time()
    keeper.tick(now)
    first = _note(gists, room.gist)["t"]
    keeper.tick(now + 3600)
    assert _note(gists, room.gist)["t"] == first
    keeper.tick(now + identity.POINTER_LIFE - identity.REFRESH_LEFT + 60)
    assert _note(gists, room.gist)["t"] > first and len(gists) == 1  # the same gist, written again


def test_a_room_that_isnt_findable_publishes_nothing(host):
    net, slug, room = host
    room.configure(findable=False)
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    assert room.url and not gists


def test_a_host_who_hasnt_connected_github_writes_nothing(host):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net, token=None)
    keeper.tick()
    assert room.url and not gists and not room.publish_error


def test_a_room_shared_at_its_own_address_needs_no_tunnel(host):
    net, slug, room = host
    room.open_invite(keep=True, mode="address", address="http://192.168.1.20:6481", port=6481)
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    assert share["tunnel"] is None and not gists


def _member(host_net, slug, room, tmp_path, url):
    """A member's computer, linked to the room at `url` (joined with the room's key)."""
    token, name = room.join("alex", "")
    guest = RomNetwork(tmp_path / "g.db", tmp_path / "g")
    gslug = _project(guest, "game-together")
    client = TestClient(collab.host_app(host_net, host_net.rooms, tunnel=True), base_url=url)
    link = guest.link_project(gslug, {"url": url, "room": slug, "token": token, "member": name, "key": room.key},
                              client=client, start=False)
    return guest, gslug, link


def test_a_member_follows_the_room_to_its_new_address(host, tmp_path):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    old = room.url
    guest, gslug, link = _member(net, slug, room, tmp_path, old)
    link.github = gh
    link.gist = room.gist
    share["tunnel"].up = False  # the host's app restarts: a new tunnel, a new address
    keeper.tick()
    new = room.url
    assert new != old
    now = time.time()
    link.last_ok = now - 5
    assert not link.find_again(now)  # it was reachable a moment ago: no need to look yet
    link.last_ok = now - link.AWAY - 1
    assert link.find_again(now)
    assert link.url == new and guest.cfg(guest.project_row(gslug))["link"]["url"] == new
    assert not link.find_again(now + 1)  # asked a moment ago


def test_a_member_doesnt_follow_a_note_the_room_didnt_sign(host, tmp_path):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    guest, gslug, link = _member(net, slug, room, tmp_path, room.url)
    link.github = gh
    link.gist = room.gist
    import json
    note = {**_note(gists, room.gist), "url": "https://elsewhere.trycloudflare.com"}  # changed after signing
    gists[room.gist]["files"][identity.NOTE]["content"] = json.dumps(note)
    link.last_ok = 0
    assert not link.find_again() and link.url == room.url


def test_a_member_doesnt_follow_to_a_room_that_cant_show_the_key(host, tmp_path):
    net, slug, room = host
    keeper, share, gh, gists = _keeper(net)
    keeper.tick()
    guest, gslug, link = _member(net, slug, room, tmp_path, room.url)
    link.github = gh
    link.gist = room.gist
    other = identity.new_secret()
    link.key = identity.public_key(other)  # this member joined another room, which says it's at this host's address
    link.gist, _ = identity.publish(other, room.url.replace("tunnel", "other"), TOKEN, "", gh)
    link.last_ok = 0
    assert not link.find_again() and link.url == room.url
