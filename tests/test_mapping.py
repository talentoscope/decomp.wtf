"""Original small MIPS fixtures: profiles, isolated banks, and history-preserving rescans."""
import copy
import json
import sqlite3
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError
from decompnet.rom.mapping import MappingError, Profile, digest, prepare

# Two leaf functions at the SAME runtime address in different overlays.
# jr ra; addu/subu v0,a0,a1 (the result is computed in the return's delay slot).
ADD = struct.pack("<II", 0x03E00008, 0x00851021)
SUB = struct.pack("<II", 0x03E00008, 0x00851023)
IMAGE = ADD + SUB + b"AAAA" + b"BBBB"


def region(name, offset=0, size=8, vram=0x80100000, kind="code", **kwargs):
    return dict(id=name, source_sha256=digest(IMAGE), offset=offset, size=size, vram=vram,
                kind=kind, confidence="verified", evidence="Original synthetic instruction fixture", **kwargs)


def profile(*regions):
    return dict(version=1, platform="psx", title="Mapping test", gp=0,
                input=dict(format="binary", sha256=digest(IMAGE)), regions=list(regions) or [region("add")])


@pytest.fixture
def mapped(tmp_path):
    image = tmp_path / "own.bin"
    image.write_bytes(IMAGE)
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    obj = profile(region("add", visible=["data_a"]), region("data_a", 16, 4, 0x80110000, "data"))
    result = net.mapping.create(image, json.dumps(obj), confirmed=True, log=lambda *_: None)
    return net, result["slug"], image, obj


def snapshot(net):
    return {t: [tuple(r) for r in net.store.all(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("projects", "functions", "attempts", "notes", "claims", "symbols", "context_versions", "events")}


@pytest.mark.parametrize("change", [
    lambda o: o.update(version=2),
    lambda o: o.update(platform="ps2"),
    lambda o: o.update(arbitrary_command="echo unsafe"),
    lambda o: o["input"].update(sha256="invalid"),
    lambda o: o["regions"][0].update(offset=True),
    lambda o: o["regions"][0].update(size=7),
    lambda o: o["regions"][0].update(vram=0x801FFFFC),
    lambda o: o["regions"][0].update(visible=["missing"]),
])
def test_profile_rejects_invalid_or_unsupported_layouts(change):
    obj = profile()
    change(obj)
    with pytest.raises(MappingError):
        Profile.parse(json.dumps(obj))


def test_duplicate_keys_and_disc_path_traversal_are_rejected():
    with pytest.raises(MappingError, match="duplicate"):
        Profile.parse("version: 1\nversion: 2\n")
    obj = profile()
    obj["input"]["format"] = "psx-disc"
    for path in ("../game.bin", "/game.bin", "A//B", "A/./B", "C:\\game.bin"):
        obj["regions"][0]["file"] = path
        with pytest.raises(MappingError, match="relative ISO9660"):
            Profile.parse(json.dumps(obj))


def test_overlapping_banks_are_valid_but_cannot_be_visible_together():
    obj = profile(region("a"), region("b", 8))
    assert len(Profile.parse(json.dumps(obj)).regions) == 2
    obj["regions"][0]["visible"] = ["b"]
    with pytest.raises(MappingError, match="overlap"):
        Profile.parse(json.dumps(obj))


def test_input_and_container_bounds_are_checked_before_import(tmp_path):
    image = tmp_path / "input.bin"
    image.write_bytes(IMAGE + b"changed")
    net = RomNetwork(tmp_path / "db", tmp_path / "work")
    with pytest.raises(NetworkError, match="input SHA-256"):
        net.mapping.create(image, json.dumps(profile()), confirmed=True)
    assert not net.store.all("SELECT * FROM projects")
    image.write_bytes(IMAGE)
    bad = profile(region("bad", offset=24, size=8))
    with pytest.raises(NetworkError, match="exceeds the input"):
        net.mapping.create(image, json.dumps(bad), confirmed=True)
    assert not net.store.all("SELECT * FROM projects")
    assert list((tmp_path / "work").iterdir()) == []


def test_derived_profiles_can_be_analysed_but_never_applied(tmp_path):
    image = tmp_path / "input.bin"
    image.write_bytes(IMAGE)
    obj = profile()
    obj["regions"][0]["confidence"] = "derived"
    p = Profile.parse(json.dumps(obj))
    assert prepare(p, image, tmp_path / "analysis").functions["add"][0].size == 8
    net = RomNetwork(tmp_path / "db", tmp_path / "work")
    with pytest.raises(NetworkError, match="preview-only"):
        net.mapping.create(image, p.render(), confirmed=True)


def test_ownership_confirmation_is_required(mapped):
    net, _, image, obj = mapped
    with pytest.raises(NetworkError, match="legally obtained"):
        net.mapping.create(image, json.dumps(obj))


def test_scoped_memory_symbols_and_address_lookup(mapped):
    net, slug, image, obj = mapped
    obj["regions"] += [region("sub", 8, visible=["data_b"]), region("data_b", 20, 4, 0x80110000, "data")]
    assert net.mapping.rescan(slug, image, json.dumps(obj), apply=True)["added_functions"] == 1
    proj = net.project_row(slug)
    assert net.reader(proj, "add")(0x80110000, 4) == b"AAAA"
    assert net.reader(proj, "sub")(0x80110000, 4) == b"BBBB"
    assert net.symbols(proj["id"], "add").label(0x80110000) == "data_a__data"
    assert net.symbols(proj["id"], "sub").label(0x80110000) == "data_b__data"
    with pytest.raises(NetworkError, match="specify"):
        net.reader(proj)
    with pytest.raises(NetworkError, match="several overlays"):
        net.get_function("0x80100000", slug)
    assert "41 41 41 41" in net.read_data(slug, "0x80110000", 4, "add")
    assert net.function_detail(slug, "sub__func_80100000")["calls"] == []
    assert len(net.twins.prints(proj)) == 2


def test_real_compiles_match_each_bank_and_rescan_preserves_history(mapped):
    from decompnet import toolchains
    if not toolchains.installed("gcc-2.7.2-psx"):
        pytest.skip("needs the app's PS1 compiler")
    net, slug, image, obj = mapped
    agent, _ = net.local_agent("original-test-fixture")
    source = "int add__func_80100000(int a, int b) { return a + b; }"
    result = net.compile_and_diff(agent, "add__func_80100000", source, slug)
    assert result.startswith("✅ MATCH"), result
    net.add_note(agent, slug, "Keep this original fixture's history", "add__func_80100000")
    saved = {t: [tuple(r) for r in net.store.all(f"SELECT * FROM {t} ORDER BY rowid")]
             for t in ("functions", "attempts", "notes")}
    obj["regions"] += [region("sub", 8)]
    # A successful match schedules its identical-function helper asynchronously.
    # Applying waits for that real helper to finish, as an operator must do.
    deadline = time.monotonic() + 5
    while net.project_row(slug)["id"] in net.twins._busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert net.mapping.rescan(slug, image, json.dumps(obj), apply=True)["preserved_functions"] == 1
    for table, rows in saved.items():
        now = [tuple(r) for r in net.store.all(f"SELECT * FROM {table} ORDER BY rowid")]
        assert now[:len(rows)] == rows
    # This must use the second bank's target despite both functions occupying 0x80100000.
    good = "int sub__func_80100000(int a, int b) { return a - b; }"
    assert net.compile_and_diff(agent, "sub__func_80100000", good, slug).startswith("✅ MATCH")
    wrong = "int sub__func_80100000(int a, int b) { return a + b; }"
    assert not net.compile_and_diff(agent, "sub__func_80100000", wrong, slug).startswith("✅ MATCH")


def test_preview_has_no_project_side_effects_and_noop_keeps_ids(mapped):
    net, slug, image, obj = mapped
    before = snapshot(net)
    obj["regions"] += [region("sub", 8)]
    plan = net.mapping.rescan(slug, image, json.dumps(obj))
    assert plan["added_regions"] == ["sub"] and plan["added_functions"] == 1 and not plan["applied"]
    assert snapshot(net) == before
    assert net.mapping.rescan(slug, image, json.dumps(profile(*obj["regions"][:2])), apply=True)["added_functions"] == 0
    assert [tuple(r) for r in net.store.all("SELECT * FROM functions")] == before["functions"]


def test_changed_targets_and_active_claims_block_application(mapped):
    net, slug, image, obj = mapped
    original = copy.deepcopy(obj)
    obj["regions"][0]["offset"] = 8
    before = snapshot(net)
    assert net.mapping.rescan(slug, image, json.dumps(obj))["blocked"]
    with pytest.raises(NetworkError, match="existing"):
        net.mapping.rescan(slug, image, json.dumps(obj), apply=True)
    assert snapshot(net) == before
    agent, _ = net.local_agent("busy")
    net.claim(agent, slug, "add__func_80100000")
    original["regions"] += [region("sub", 8)]
    before = snapshot(net)
    with pytest.raises(NetworkError, match="claims released"):
        net.mapping.rescan(slug, image, json.dumps(original), apply=True)
    assert snapshot(net) == before


def test_transaction_failure_does_not_publish_new_targets_or_image(mapped, monkeypatch):
    net, slug, image, obj = mapped
    obj["regions"] += [region("sub", 8)]
    before = snapshot(net)
    revisions = set((Path(net.project_row(slug)["root"]) / ".mapping").iterdir())
    def fail(*_):
        raise sqlite3.IntegrityError("injected persistence failure")
    monkeypatch.setattr(net.mapping, "_store_symbols", fail)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        net.mapping.rescan(slug, image, json.dumps(obj), apply=True)
    assert snapshot(net) == before
    assert set((Path(net.project_row(slug)["root"]) / ".mapping").iterdir()) == revisions


@pytest.mark.parametrize("raw", [False, True])
def test_disc_profiles_use_file_offsets_and_verify_container_hash(tmp_path, raw):
    from tests.test_overlays import _iso
    iso = _iso({"CODE.BIN": b"HEAD" + ADD + SUB})
    # A Mode 2 raw BIN has 24 bytes before each sector's 2048-byte user data.
    if raw:
        iso = b"".join(bytes(24) + iso[i:i+2048] + bytes(280) for i in range(0, len(iso), 2048))
    image = tmp_path / "disc.bin"
    image.write_bytes(iso)
    obj = profile(region("disc_code", offset=4, file="code.bin"))
    obj["input"] = dict(format="psx-disc", sha256=digest(iso))
    obj["regions"][0]["source_sha256"] = digest(b"HEAD" + ADD + SUB)
    p = Profile.parse(json.dumps(obj))
    assert prepare(p, image, tmp_path / "analysis").data["disc_code"] == ADD
    obj["regions"][0]["source_sha256"] = "0" * 64
    with pytest.raises(MappingError, match="source hash"):
        prepare(Profile.parse(json.dumps(obj)), image, tmp_path / "bad")


def test_truncated_or_malformed_disc_directory_is_rejected(tmp_path):
    from tests.test_overlays import _iso
    iso = bytearray(_iso({"CODE.BIN": ADD}))
    # The root directory promises a file extent beyond the end of the disc.
    struct.pack_into("<I", iso, 18 * 2048 + 2 * 34 + 2, 0x10000)
    image = tmp_path / "bad.iso"
    image.write_bytes(iso)
    obj = profile(region("bad", file="CODE.BIN"))
    obj["input"] = dict(format="psx-disc", sha256=digest(iso))
    with pytest.raises(MappingError, match="outside the image"):
        prepare(Profile.parse(json.dumps(obj)), image, tmp_path / "extent")
    iso[18 * 2048] = 1  # an impossibly short directory record
    image.write_bytes(iso)
    obj["input"]["sha256"] = digest(iso)
    with pytest.raises(MappingError, match="malformed"):
        prepare(Profile.parse(json.dumps(obj)), image, tmp_path / "directory")


def test_function_boundary_hints_are_enforced(tmp_path):
    image = tmp_path / "own.bin"
    image.write_bytes(IMAGE)
    obj = profile(region("bad_hint", functions=[dict(offset=0, size=4)]))
    with pytest.raises(MappingError, match="hint|outside|overlap"):
        prepare(Profile.parse(json.dumps(obj)), image, tmp_path / "analysis")


def test_unsafe_layout_operations_and_room_sharing_are_disabled(mapped):
    from decompnet.app.collab import Room
    from decompnet.app.tools import project_info
    net, slug, _, _ = mapped
    agent, _ = net.local_agent("operator")
    with pytest.raises(NetworkError, match="mapping v1"):
        net.rename_symbol(agent, slug, "add__func_80100000", "renamed")
    with pytest.raises(NetworkError, match="profile; v1"):
        net.split_function(agent, slug, "add__func_80100000", "0x80100004", "tail")
    with pytest.raises(NetworkError, match="source-tree"):
        net.export_rom(slug)
    with pytest.raises(NetworkError, match="overlay-aware"):
        Room(net, slug)
    assert not net.overview(slug)["exportable"]
    assert "read_data(unit=...)" in project_info(net, slug)


def test_background_helpers_block_apply_and_corrupt_storage_is_detected(mapped):
    net, slug, image, obj = mapped
    net.twins.jobs[slug] = SimpleNamespace(state="running")
    with pytest.raises(NetworkError, match="identical-function"):
        net.mapping.rescan(slug, image, json.dumps(obj), apply=True)
    net.twins.jobs.clear()
    proj = net.project_row(slug)
    (Path(proj["root"]) / net.cfg(proj)["rom"]["file"]).write_bytes(b"corrupted")
    with pytest.raises(NetworkError, match="stored mapping image"):
        net.mapping.rescan(slug, image, json.dumps(obj), apply=True)


def test_twin_transfer_renames_data_for_the_correct_bank(tmp_path):
    from decompnet import toolchains
    if not toolchains.installed("gcc-2.7.2-psx"):
        pytest.skip("needs the app's PS1 compiler")
    load = struct.pack("<IIII", 0x3C028011, 0x8C420000, 0x03E00008, 0)
    blob = load + load + b"AAAA" + b"BBBB"
    image = tmp_path / "own.bin"
    image.write_bytes(blob)
    obj = profile(region("a", size=16, visible=["data_a"]), region("b", 16, 16, visible=["data_b"]),
                  region("data_a", 32, 4, 0x80110000, "data"), region("data_b", 36, 4, 0x80110000, "data"))
    obj["input"]["sha256"] = digest(blob)
    for r in obj["regions"]:
        r["source_sha256"] = digest(blob)
    net = RomNetwork(tmp_path / "db", tmp_path / "work")
    slug = net.mapping.create(image, json.dumps(obj), confirmed=True)["slug"]
    agent, _ = net.local_agent("test")
    source = "extern int data_a__data; int a__func_80100000(void) { return data_a__data; }"
    assert net.compile_and_diff(agent, "a__func_80100000", source, slug).startswith("✅ MATCH")
    a, b = [net.store.one("SELECT * FROM functions WHERE name=?", n) for n in ("a__func_80100000", "b__func_80100000")]
    transferred = net.twins.source_for(net.project_row(slug), a, b)
    assert "data_b__data" in transferred and "data_a__data" not in transferred
    assert net.compile_and_diff(agent, b["name"], transferred, slug).startswith("✅ MATCH")


def test_cli_offline_validation_and_live_workspace_guard(tmp_path, monkeypatch, capsys):
    from decompnet.app import cli, mapping_cli
    image, config = tmp_path / "own.bin", tmp_path / "map.json"
    image.write_bytes(IMAGE)
    config.write_text(json.dumps(profile()))
    args = SimpleNamespace(action="validate", image=image, profile=config, workspace=None, project=None, own_copy=False)
    assert mapping_cli.run(args) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    args.action, args.workspace = "create", tmp_path / "live"
    state = args.workspace / ".decomp/server.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"port": 12345}')
    monkeypatch.setattr(cli, "_running_version", lambda _: "0.10.6")
    assert mapping_cli.run(args) == 1
    assert "server is running" in capsys.readouterr().err
    assert not (state.parent / "decomp.db").exists()


def test_operator_api_security_preview_and_apply(mapped, tmp_path):
    from decompnet.app.server import build
    from starlette.testclient import TestClient
    net, slug, image, obj = mapped
    mcp, manager, security = build(net, tmp_path / "home", 6499, "test-secret")
    app = mcp.streamable_http_app(transport_security=security, host="localhost")
    with TestClient(app, base_url="http://localhost:6499") as client:
        payload = dict(action="profile", slug=slug)
        assert client.post("/api/mapping", json=payload, headers={"X-Decomp": "1"}).status_code == 401
        client.cookies.set("decomp_live", "test-secret")
        assert client.post("/api/mapping", json=payload).status_code == 403
        h = {"X-Decomp": "1"}
        assert client.post("/api/mapping", json=payload, headers=h).json() == Profile.parse(json.dumps(obj)).to_dict()
        obj["regions"] += [region("sub", 8)]
        payload.update(action="preview", image=str(image), profile=json.dumps(obj))
        before = snapshot(net)
        def job_request():
            jid = client.post("/api/mapping", json=payload, headers=h).json()["job"]
            for _ in range(100):
                result = client.get(f"/api/job?id={jid}").json()
                if result["state"] != "running":
                    return result
                time.sleep(0.01)
            pytest.fail("mapping job did not finish")
        result = job_request()
        assert result["state"] == "done" and result["result"]["added_regions"] == ["sub"]
        assert snapshot(net) == before
        manager.sessions[99] = SimpleNamespace(slug=slug, state="working", stop=threading.Event())
        payload["action"] = "apply"
        assert "pause" in job_request()["error"]
        manager.sessions[99].stop.set()  # stop requested is not the same as finished
        assert "pause" in job_request()["error"]
        manager.sessions.clear()
        result = job_request()
        assert result["state"] == "done" and result["result"]["applied"]
