"""Operator SMS/GG profile changes and source-acceptance gates."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..network import NetworkError
from ..rom import sms_identity, sms_profile


class SMSProjects:
    def __init__(self, net):
        self.net = net

    def require(self, project=None, function=None, *, row=None):
        net = self.net
        proj = net.store.one("SELECT * FROM projects WHERE id=?", row["id"]) if row is not None else \
            net._project(project) if project or not function else net.store.one(
            "SELECT * FROM projects WHERE id=?", net._function(function, None)["project_id"])
        if proj is None:
            raise NetworkError("the project is no longer available")
        try:
            sms_profile.require_supported(net.cfg(proj))
        except ValueError as exc:
            raise NetworkError(str(exc)) from None

    def inspect(self, slug):
        cfg = self.net.cfg(self.net._project(slug))
        view = sms_profile.describe(cfg)
        if view is None or cfg.get("kind") != "rom":
            raise NetworkError("cartridge profiles are for SMS/GG ROM projects")
        return {**view, "mappers": list(sms_profile.MAPPERS), "variants": list(sms_profile.VARIANTS)}

    def brief(self, proj):
        view = sms_profile.describe(self.net.cfg(proj))
        profile = view["profile"] or {}
        return [f"Cartridge mapping: {profile.get('mapper', 'unknown')} ({view['status']}); "
                f"build intent: {profile.get('variant', 'unknown')}.", view["message"]]

    @staticmethod
    def _preserve_sources(db, pid, old, new):
        if old["mapper"] != new["mapper"] and new["mapper"] not in ("unknown", "unsupported") and db.execute(
                "SELECT 1 FROM functions WHERE project_id=? AND status IN ('matched','documented')", (pid,)).fetchone():
            raise NetworkError("changing mapping with accepted source requires revalidation; existing work is retained")

    @staticmethod
    def _bind_legacy_identity(db, pid, cfg, image):
        if cfg["rom"].get("identity") is not None:
            return
        for fn in db.execute("SELECT rom_offset, target FROM functions WHERE project_id=? AND rom_offset IS NOT NULL", (pid,)):
            target = bytes(fn["target"] or b"")
            if fn["rom_offset"] < 0 or image[fn["rom_offset"]:fn["rom_offset"] + len(target)] != target:
                raise NetworkError("legacy ROM differs from stored function targets; retain the original revision")
        cfg["rom"]["identity"] = sms_identity.identify(image, image, {**cfg["rom"], "platform": cfg["platform"]})

    def follow_host(self, slug, profile):
        """Apply a declaration from the authenticated room state only for this exact local revision."""
        net = self.net
        if slug not in net.links:
            raise NetworkError("host profile updates require a linked room")
        proj = net._project(slug)
        with net.store.tx() as db:
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            if cfg.get("platform") not in ("sms", "gg"):
                raise NetworkError("host cartridge profiles are for SMS/GG projects")
            try:
                old = sms_profile.read(cfg)
                if cfg["rom"].get("identity") is None:
                    image = sms_identity.strip_copier_header((Path(proj["root"]) / cfg["rom"]["file"]).read_bytes())
                    sms_identity.verify_normalized(image, None, cfg["rom"].get("sha1"))
                    self._bind_legacy_identity(db, proj["id"], cfg, image)
                incoming = sms_profile.read({**cfg, "sms_profile": profile})
            except (ValueError, TypeError) as exc:
                raise NetworkError(str(exc)) from None
            if incoming == old:
                return
            self._preserve_sources(db, proj["id"], old, incoming)
            cfg["sms_profile"] = incoming
            cfg.pop("export", None)
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        net._progress_cache.pop(proj["id"], None)

    def configure(self, slug: str, mapper: str, variant: str, reason: str) -> dict:
        net = self.net
        proj = net._project(slug)
        if slug in net.links or net.cfg(proj).get("link"):
            raise NetworkError("the room host selects cartridge mapping")
        self.inspect(slug)
        if not isinstance(reason, str):
            raise NetworkError("profile reason must be text")
        reason = " ".join(reason.split())
        if not isinstance(mapper, str) or not isinstance(variant, str):
            raise NetworkError("mapper and variant must be text")
        cfg = net.cfg(proj)
        image = sms_identity.strip_copier_header((Path(proj["root"]) / cfg["rom"]["file"]).read_bytes())
        try:
            sms_identity.verify_normalized(image, cfg["rom"].get("identity"), cfg["rom"].get("sha1"))
        except ValueError as exc:
            raise NetworkError(str(exc)) from None
        ap = net.autopilots.get(slug)
        job = net.twins.jobs.get(slug)
        if (ap and ap.get("state") == "running") or (net.permuter.status(slug) or {}).get("state") == "running" or \
                (job and job.state == "running") or proj["id"] in net.twins._busy:
            raise NetworkError("stop this project's background analysis before changing its profile")
        with net.store.tx() as db:
            current = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            if current["rom"] != cfg["rom"]:
                raise NetworkError("ROM metadata changed while selecting its profile; retry with the current revision")
            if db.execute("SELECT 1 FROM claims c JOIN functions f ON f.id=c.function_id WHERE f.project_id=? "
                          "AND c.expires_at>?", (proj["id"], time.time())).fetchone():
                raise NetworkError("release this project's claims before changing its profile")
            try:
                old = sms_profile.read(current)
            except (ValueError, TypeError) as exc:
                raise NetworkError(str(exc)) from None
            self._bind_legacy_identity(db, proj["id"], current, image)
            profile = {"version": 1, "mapper": mapper, "variant": variant, "origin": "operator", "reason": reason,
                       "sha256": hashlib.sha256(image).hexdigest(), "size": len(image)}
            candidate = {**current, "sms_profile": profile}
            try:
                sms_profile.read(candidate)
            except (ValueError, TypeError) as exc:
                raise NetworkError(str(exc)) from None
            # A profile edit cannot reinterpret previously accepted source without a resplit/revalidation workflow.
            self._preserve_sources(db, proj["id"], old, profile)
            current["sms_profile"] = profile
            current.pop("export", None)  # cached verification described the earlier profile/intent
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(current), proj["id"]))
            net._event(db, proj["id"], None, "profile", f"SMS/GG mapping {mapper}; build intent {variant}: {reason}")
        net._progress_cache.pop(proj["id"], None)
        return self.inspect(slug)
