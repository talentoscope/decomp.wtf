"""Mapping declarations bind a revision; unknown mappings cannot silently acquire accepted source."""
import copy
import json

import pytest
from starlette.testclient import TestClient

from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError
from decompnet.rom import sms_profile

from .test_sms import make_rom

REASON = "The authored fixture specifies Sega cartridge mapping."


@pytest.fixture
def project(tmp_path, monkeypatch):
    from decompnet.rom import gb_build
    monkeypatch.setattr(gb_build, "available", lambda _arch=None: False)
    net = RomNetwork(tmp_path / "state.db", tmp_path / "projects")
    rom = make_rom(code={0: bytes.fromhex("cd0002" "18fe"), 0x200: bytes.fromhex("3e01c9")})
    slug = net.import_rom(rom, name="fixture.sms", confirmed=True, log=lambda *_: None)
    agent, _ = net.local_agent("researcher")
    name = net.store.one("SELECT name FROM functions WHERE project_id=? AND rom_offset=?", net.project_row(slug)["id"], 0x200)["name"]
    return net, slug, agent, name


def test_unknown_and_legacy_are_readable_but_cannot_claim_or_accept_source(project):
    net, slug, agent, name = project
    for legacy in (False, True):
        if legacy:
            net._update_cfg(slug, lambda cfg: cfg.pop("sms_profile"))
        before = net.cfg(net.project_row(slug))
        assert net.overview(slug)["sms_profile"]["status"] == ("legacy" if legacy else "unresolved")
        assert name in net.get_function(name, slug)
        for operation in (lambda: net.claim(agent, slug, name), lambda: net.claim_row(agent, slug, name),
                          lambda: net.claim_asm_row(agent, slug, name), lambda: net.attempt(agent, name, "bad", slug),
                          lambda: net.document_function(agent, slug, name, "bad")):
            with pytest.raises(NetworkError, match="Declare the cartridge mapping"):
                operation()
        assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
        assert net.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
        assert net.cfg(net.project_row(slug)) == before  # loading a legacy project is not a silent migration


def test_declaring_sega_enables_work_and_preserves_revision_and_notes(project):
    net, slug, agent, name = project
    before = net.cfg(net.project_row(slug))
    net.add_note(agent, slug, "Retain this investigation and its supporting evidence.")
    result = net.sms.configure(slug, "sega", "annotated-original", REASON)
    assert result["supported"] and result["status"] == "declared"
    assert result["profile"]["origin"] == "operator" and result["profile"]["reason"] == REASON
    assert net.cfg(net.project_row(slug))["rom"] == before["rom"]
    net.sms.require(slug)
    assert "Retain" in net.store.one("SELECT text FROM notes")["text"]
    assert "CLAIMED" in net.claim(agent, slug, name, track="asm")
    held = net.store.one("SELECT * FROM claims")
    with pytest.raises(NetworkError, match="release this project's claims"):
        net.sms.configure(slug, "unknown", "experimental", REASON)
    assert net.store.one("SELECT * FROM claims") == held


@pytest.mark.parametrize("mapper", ["mapperless", "codemasters", "unsupported", "unknown"])
def test_recording_a_mapper_does_not_invent_implementation_support(project, mapper):
    net, slug, _agent, _name = project
    result = net.sms.configure(slug, mapper, "experimental", REASON)
    assert not result["supported"] and not result["exact_original_intent"]
    with pytest.raises(NetworkError):
        net.sms.require(slug)


@pytest.mark.parametrize("field,value", [("version", 2), ("version", True), ("mapper", "made-up"),
    ("variant", "made-up"), ("size", 1), ("sha256", "0" * 64), ("sha256", None), ("origin", "automatic")])
def test_invalid_or_future_profiles_cannot_be_accepted(project, field, value):
    net, slug, _agent, _name = project
    net.sms.configure(slug, "sega", "annotated-original", REASON)
    cfg = net.cfg(net.project_row(slug))
    cfg["sms_profile"][field] = value
    with pytest.raises(ValueError):
        sms_profile.read(cfg)
    assert not sms_profile.describe(cfg)["supported"]


def test_profile_changes_cannot_reinterpret_accepted_work_or_stale_rows(project):
    net, slug, _agent, name = project
    net.sms.configure(slug, "sega", "annotated-original", REASON)
    proj = net.project_row(slug)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='documented', matched_source='ret' WHERE project_id=? AND name=?", (proj["id"], name))
    saved = net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name)
    net.sms.configure(slug, "unknown", "experimental", REASON)
    with pytest.raises(NetworkError, match="Declare the cartridge mapping"):
        net.sms.require(row=proj)  # an old caller snapshot cannot bypass the current gate
    with pytest.raises(NetworkError, match="requires revalidation"):
        net.sms.configure(slug, "sega", "annotated-original", REASON)
    with pytest.raises(NetworkError, match="Declare the cartridge mapping"):
        net.review_documented(slug, name, True)
    assert net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name) == saved


def test_reimport_with_new_tools_keeps_the_declared_profile_and_function_history(project, monkeypatch):
    from pathlib import Path
    from decompnet.rom import gb_build
    net, slug, _agent, _name = project
    net.sms.configure(slug, "sega", "experimental", REASON)
    before = net.cfg(net.project_row(slug))
    ids = [r["id"] for r in net.store.all("SELECT id FROM functions")]
    image = (Path(net.project_row(slug)["root"]) / before["rom"]["file"]).read_bytes()
    monkeypatch.setattr(gb_build, "available", lambda _arch=None: True)
    assert net.import_rom(image, name="fixture.sms", confirmed=True, log=lambda *_: None) == slug
    assert net.cfg(net.project_row(slug))["sms_profile"] == before["sms_profile"]
    assert [r["id"] for r in net.store.all("SELECT id FROM functions")] == ids


def test_wrong_revision_and_invalid_changes_leave_config_unchanged(project):
    net, slug, _agent, _name = project
    before = net.cfg(net.project_row(slug))
    with pytest.raises(NetworkError, match="20 to 1000"):
        net.sms.configure(slug, "sega", "annotated-original", "guess")
    assert net.cfg(net.project_row(slug)) == before
    from pathlib import Path
    image = Path(net.project_row(slug)["root"]) / before["rom"]["file"]
    changed = bytearray(image.read_bytes())
    changed[0x200] ^= 1
    image.write_bytes(changed)
    with pytest.raises(NetworkError, match="frozen normalized identity"):
        net.sms.configure(slug, "sega", "annotated-original", REASON)
    assert net.cfg(net.project_row(slug)) == before


def test_legacy_profile_selection_binds_identity_and_preserves_other_config(project):
    net, slug, _agent, _name = project
    def legacy(cfg):
        cfg.pop("sms_profile")
        cfg["rom"].pop("identity")
    net._update_cfg(slug, legacy)
    before = copy.deepcopy(net.cfg(net.project_row(slug)))
    net.sms.configure(slug, "sega", "annotated-original", REASON)
    after = net.cfg(net.project_row(slug))
    assert after["sms_profile"]["sha256"] == after["rom"]["identity"]["normalized"]["sha256"]
    after.pop("sms_profile")
    after["rom"].pop("identity")
    assert after == before


def test_newer_profile_remains_readable_and_cannot_be_overwritten(project):
    net, slug, _agent, _name = project
    net._update_cfg(slug, lambda cfg: cfg["sms_profile"].update(version=2))
    before = net.cfg(net.project_row(slug))
    assert net.overview(slug)["sms_profile"]["status"] == "invalid"
    with pytest.raises(NetworkError, match="unsupported SMS/GG profile version"):
        net.sms.configure(slug, "sega", "annotated-original", REASON)
    assert net.cfg(net.project_row(slug)) == before


@pytest.mark.parametrize("version", [True, 2])
def test_a_future_or_invalid_input_identity_cannot_qualify_a_profile(project, version):
    net, slug, _agent, _name = project
    net.sms.configure(slug, "sega", "annotated-original", REASON)
    net._update_cfg(slug, lambda cfg: cfg["rom"]["identity"].update(version=version))
    with pytest.raises(NetworkError, match="ROM identity version"):
        net.sms.require(slug)


def test_legacy_profile_cannot_freeze_a_changed_function_target(project):
    from pathlib import Path
    net, slug, _agent, _name = project
    def legacy(cfg):
        cfg.pop("sms_profile")
        cfg["rom"].pop("identity")
        cfg["rom"].pop("sha1")
    net._update_cfg(slug, legacy)
    before = net.cfg(net.project_row(slug))
    path = Path(net.project_row(slug)["root"]) / before["rom"]["file"]
    changed = bytearray(path.read_bytes())
    changed[0x200] ^= 1
    path.write_bytes(changed)
    with pytest.raises(NetworkError, match="differs from stored function targets"):
        net.sms.configure(slug, "sega", "annotated-original", REASON)
    assert net.cfg(net.project_row(slug)) == before


def test_operator_profile_route_is_guarded_and_not_a_worker_tool(project, tmp_path):
    net, slug, _agent, _name = project
    port, secret = 6498, "profile-test-secret"
    mcp, _manager, security = build(net, tmp_path, port, secret)
    app = mcp.streamable_http_app(transport_security=security, host="127.0.0.1")
    with TestClient(app, base_url=f"http://127.0.0.1:{port}") as client:
        assert client.get(f"/api/sms/profile?slug={slug}").status_code == 401
        client.cookies.set("decomp_live", secret)
        assert client.get(f"/api/sms/profile?slug={slug}").json()["status"] == "unresolved"
        data = {"slug": slug, "mapper": "sega", "variant": "annotated-original", "reason": REASON}
        assert client.post("/api/sms/profile", json=data).status_code == 403
        result = client.post("/api/sms/profile", json=data, headers={"X-Decomp": "1"})
        assert result.status_code == 200 and result.json()["supported"]
        assert client.get(f"/api/project?slug={slug}").json()["sms_profile"]["status"] == "declared"
        assert "SMSProfileUI" in client.get("/assets/sms-profile.js").text
        assert '"configure_sms_profile"' not in json.dumps([tool.name for tool in mcp._tool_manager.list_tools()])


def test_guest_follows_only_revision_bound_host_profiles(project, tmp_path):
    from pathlib import Path
    from decompnet.app import collab
    host, slug, _agent, _name = project
    host.sms.configure(slug, "sega", "annotated-original", REASON)
    host.rooms[slug] = collab.Room(host, slug)
    invite = host.rooms[slug].open_invite()
    guest = RomNetwork(tmp_path / "guest.db", tmp_path / "guest")
    image = Path(host.project_row(slug)["root"]) / host.cfg(host.project_row(slug))["rom"]["file"]
    with TestClient(collab.host_app(host, host.rooms), base_url="https://room.example") as client:
        gslug = collab.join_room(guest, f"https://room.example/join/{invite}#room={slug}", "guest",
                                image.read_bytes(), log=lambda *_: None, client=client, start=False)
        guest.links[gslug].sync()
        assert guest.sms.inspect(gslug)["supported"]
        with pytest.raises(NetworkError, match="room host"):
            guest.sms.configure(gslug, "unknown", "experimental", REASON)
        before = guest.cfg(guest.project_row(gslug))
        wrong = {**host.cfg(host.project_row(slug))["sms_profile"], "sha256": "0" * 64}
        with pytest.raises(NetworkError, match="another revision"):
            guest.links[gslug].apply({"sms_profile": wrong})
        assert guest.cfg(guest.project_row(gslug)) == before
        host.sms.configure(slug, "unknown", "experimental", REASON)
        guest.links[gslug].sync()
        assert not guest.sms.inspect(gslug)["supported"]
