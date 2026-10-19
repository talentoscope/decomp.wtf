"""Supplied dumps and normalized comparison inputs retain separate, revision-bound identities."""
import copy
import hashlib

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.rom import detect, sms
from decompnet.rom.sms_identity import identify, merge_inputs, verify_normalized

from .test_sms import make_rom


@pytest.mark.parametrize("size", [0x2000, 0x4000, 0xC000, 0x20000])
@pytest.mark.parametrize("region, platform", [(4, "sms"), (6, "gg")])
def test_detection_retains_original_and_normalized_identity(size, region, platform):
    rom = make_rom(size=size, region=region)
    raw = bytes(512) + rom
    picked = detect.identify(raw, f"input.{platform}")
    identity = picked.info["identity"]
    assert picked.data == rom and sms.strip_copier_header(raw) == rom
    assert identity == sms.read_info(raw)["identity"]
    assert identity["normalized"] == {"sha256": hashlib.sha256(rom).hexdigest(), "size": len(rom)}
    assert identity["inputs"] == [{"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "copier_header_size": 512}]
    assert identity["platform"] == platform and identity["platform_origin"] == "header"
    assert identity["product_code"] == picked.info["game_code"] and identity["revision"] == picked.info["version"]
    assert picked.info["sha1"] == hashlib.sha1(rom).hexdigest()  # existing project lookup stays compatible


def test_headerless_input_does_not_invent_a_revision_or_strip_unknown_bytes():
    rom = make_rom(size=0x2000, header=False)
    identity = sms.read_info(rom, "input.gg")["identity"]
    assert identity["platform_origin"] == "filename" and identity["revision"] is None
    assert identity["product_code"] is None and identity["inputs"][0]["copier_header_size"] == 0
    raw = bytes(512) + rom
    assert sms.strip_copier_header(raw) == raw
    with pytest.raises(ValueError):
        identify(raw, rom[:-1], {"platform": "sms"})


def test_merging_wrapper_evidence_is_idempotent_and_cannot_retarget_a_revision():
    rom = make_rom()
    first = sms.read_info(rom)["identity"]
    wrapped = sms.read_info(bytes(512) + rom)["identity"]
    saved = copy.deepcopy(first)
    merged = merge_inputs(first, wrapped)
    assert len(merged["inputs"]) == 2 and first == saved
    assert merge_inputs(merged, wrapped) == merged
    changed = bytearray(rom)
    changed[0x100] = 1
    with pytest.raises(ValueError, match="different normalized revisions"):
        merge_inputs(first, sms.read_info(bytes(changed))["identity"])
    future = {**first, "version": 2}
    with pytest.raises(ValueError, match="unsupported SMS identity version"):
        merge_inputs(future, first)
    with pytest.raises(ValueError, match="frozen normalized identity"):
        verify_normalized(bytes(changed), first)
    verify_normalized(rom, first)
    verify_normalized(rom, None, hashlib.sha1(rom).hexdigest())
    with pytest.raises(ValueError, match="frozen SHA-1"):
        verify_normalized(bytes(changed), None, hashlib.sha1(rom).hexdigest())
    with pytest.raises(ValueError, match="unsupported SMS identity version"):
        verify_normalized(rom, future)


def test_reimport_retains_wrapper_sources_without_replacing_project_work(tmp_path):
    net = RomNetwork(tmp_path / "state.db", tmp_path / "projects")
    rom = make_rom(code={0: bytes.fromhex("18fe")})
    slug = net.import_rom(rom, confirmed=True, name="input.sms", log=lambda *_: None)
    proj = net.project_row(slug)
    agent, _ = net.local_agent("researcher")
    net.add_note(agent, slug, "Preserve this revision's investigation notes.")
    functions = net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
    net.import_rom(bytes(512) + rom, confirmed=True, name="wrapped.sms", log=lambda *_: None)
    cfg = net.cfg(net.project_row(slug))
    assert len(cfg["rom"]["identity"]["inputs"]) == 2
    assert net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"]) == functions
    assert net.store.one("SELECT text FROM notes WHERE project_id=?", proj["id"])["text"].startswith("Preserve")
    assert (tmp_path / "projects" / slug / cfg["rom"]["file"]).read_bytes() == rom


def test_legacy_project_attaches_new_observation_without_replacing_frozen_metadata(tmp_path):
    net = RomNetwork(tmp_path / "state.db", tmp_path / "projects")
    rom = make_rom(code={0: bytes.fromhex("18fe")})
    slug = net.import_rom(rom, confirmed=True, name="input.sms", log=lambda *_: None)
    net._update_cfg(slug, lambda cfg: cfg["rom"].pop("identity"))
    before = net.cfg(net.project_row(slug))
    net.import_rom(bytes(512) + rom, confirmed=True, name="wrapped.sms", log=lambda *_: None)
    after = net.cfg(net.project_row(slug))
    assert len(after["rom"]["identity"]["inputs"]) == 1
    after["rom"].pop("identity")
    assert before == after
