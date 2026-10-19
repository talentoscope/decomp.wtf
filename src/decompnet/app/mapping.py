"""Original mapping-profile import and additive rescan service.

Analysis happens in a disposable directory. Application only adds immutable region
revisions and new database rows; existing function targets and history never change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from ..network import NetworkError
from ..rom import mapping, match, split
from ..rom.symbols import Symbol, SymbolTable


def scope(cfg, unit):
    """The explicitly visible region ids for a mapped unit. None means a legacy project."""
    meta = cfg.get("mapping")
    if not meta:
        return None
    regions = meta["regions"]
    if not unit:
        if len(regions) != 1:
            raise NetworkError("this mapped project has several regions; specify the function's unit/region")
        unit = next(iter(regions))
    if unit not in regions:
        raise NetworkError(f"unknown mapping region: {unit}")
    return {unit, *regions[unit]["visible"]}


def _symbols(prepared):
    result = {}
    for region in prepared.profile.regions:
        syms = [Symbol(fn.name, fn.vram, fn.size, "func") for fn in prepared.functions[region.id]]
        addresses = {s.address for s in syms}
        if region.kind == "data":
            syms.append(Symbol(region.id + "__data", region.vram, region.size))
        for s in prepared.symbols[region.id]:
            if s.vram not in addresses:
                syms.append(Symbol(region.id + "__" + s.name, s.vram, s.size, s.kind))
                addresses.add(s.vram)
        result[region.id] = syms
    return result


class MappingEngine:
    def __init__(self, net):
        self.net = net

    @staticmethod
    def _profile(text):
        try:
            return mapping.Profile.parse(text)
        except mapping.MappingError as e:
            raise NetworkError(str(e)) from None

    def _quiet(self, slug, pid, db):
        if db.execute("SELECT 1 FROM claims c JOIN functions f ON f.id=c.function_id WHERE f.project_id=?", (pid,)).fetchone():
            raise NetworkError("mapping rescan needs this project's workers stopped and its claims released")
        ap = self.net.autopilots.get(slug)
        pm = self.net.permuter.status(slug)
        if (ap and ap.get("state") == "running") or (pm and pm.get("state") == "running"):
            raise NetworkError("stop this project's autopilot and permuter before applying a mapping rescan")
        job = self.net.twins.jobs.get(slug)
        if (job and job.state == "running") or pid in self.net.twins._busy:
            raise NetworkError("wait for this project's identical-function pass to finish before applying a rescan")

    def _analysis(self, profile, image, directory, log):
        try:
            return mapping.prepare(profile, image, directory, log)
        except (mapping.MappingError, split.RomError) as e:
            raise NetworkError(str(e)) from None

    def _layout(self, prepared, existing=None, old_image=b""):
        profile = prepared.profile
        old = existing or {}
        regions = dict(old.get("regions", {}))
        data = bytearray(old_image)
        added = []
        for r in profile.regions:
            if r.id in regions:
                continue
            data.extend(bytes(-len(data) % 16))
            at = len(data)
            data.extend(prepared.data[r.id])
            regions[r.id] = r.to_dict() | {"rom_start": at, "rom_end": len(data),
                                           "bytes_sha256": mapping.digest(prepared.data[r.id])}
            added.append(r.id)
        return bytes(data), regions, added

    def _rows(self, prepared, regions):
        syms = _symbols(prepared)
        functions = []
        for r in prepared.profile.regions:
            table = SymbolTable([s for key in (r.id, *r.visible) for s in syms[key]])
            hints = {r.vram + f["offset"]: f for f in r.functions}
            for fn in prepared.functions[r.id]:
                target = match.target_from_split(fn, table.resolve, False, prepared.profile.gp)
                state = "asm" if hints.get(fn.vram, {}).get("kind") == "asm" else "open"
                functions.append(dict(name=fn.name, size=fn.size, n_insns=len(fn.lines), target=target.code,
                                      relocs=json.dumps(target.to_json()), asm=self.net._asm_source(fn),
                                      unit=r.id, vram=fn.vram, rom_offset=regions[r.id]["rom_start"] + fn.rom,
                                      status=state))
        return functions, syms

    @staticmethod
    def _store_functions(db, pid, functions):
        for f in functions:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,rom_offset,status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, *(f[k] for k in (
                           "name", "size", "n_insns", "target", "relocs", "asm", "unit", "vram", "rom_offset", "status"))))

    @staticmethod
    def _store_symbols(db, pid, symbols, units):
        for unit in units:
            for s in symbols[unit]:
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, s.name, s.address, s.size, s.kind))

    @staticmethod
    def _persist(root, prepared, image):
        """Publish a new immutable revision; the database pointer is switched only after this completes."""
        relative = Path(".mapping") / uuid.uuid4().hex
        destination = root / relative
        destination.parent.mkdir(exist_ok=True)
        try:
            os.replace(prepared.directory, destination)
            (destination / "image.bin").write_bytes(image)
            (destination / "profile.json").write_text(prepared.profile.render(), encoding="utf-8")
            # Targets and metadata are durable before publishing their pointer.
            for name in ("image.bin", "profile.json"):
                with (destination / name).open("rb") as f:
                    os.fsync(f.fileno())
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return relative

    def create(self, image: Path, text: str, *, confirmed=False, log=print):
        if not confirmed:
            raise NetworkError("confirm that this is your own legally obtained copy of the game")
        profile = self._profile(text)
        if any(r.confidence != "verified" for r in profile.regions):
            raise NetworkError("derived regions are preview-only; supply verified address evidence before import")
        self.net.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mapping-", dir=self.net.workspace) as tmp:
            prepared = self._analysis(profile, image, Path(tmp) / "analysis", log)
            flat, regions, units = self._layout(prepared)
            functions, symbols = self._rows(prepared, regions)
            if not functions:
                raise NetworkError("a mapped project needs at least one code function")
            base = re.sub(r"[^a-z0-9]+", "-", profile.title.lower()).strip("-")[:40] or "mapped-game"
            slug, n = base, 2
            while (self.net.workspace / slug).exists() or self.net.store.one("SELECT 1 FROM projects WHERE slug=?", slug):
                slug, n = f"{base}-{n}", n + 1
            root = self.net.workspace / slug
            root.mkdir()
            try:
                revision = self._persist(root, prepared, flat)
                cfg = dict(kind="rom", platform="psx", big_endian=False, gp=profile.gp,
                           rom=dict(title=profile.title, game_code="", region="profile", compiler_guess="PSYQ",
                                    sha1=hashlib.sha1(flat).hexdigest(), size=len(flat), byte_order="exe",
                                    file=str(revision / "image.bin"), program=str(revision / "image.bin")),
                           segments=[dict(name=k, rom_start=r["rom_start"], rom_end=r["rom_end"], vram=r["vram"])
                                     for k, r in regions.items()],
                           compiler=dict(toolchain="gcc-2.7.2-psx", flags=["-O2", f"-G{8 if profile.gp else 0}"], timeout=20),
                           legal=dict(basis="personal-copy", confirmed_at=time.time()), context=dict(file=None),
                           mapping=dict(version=1, profile=profile.to_dict(), regions=regions, revision=str(revision),
                                        symbol_units={s.name: unit for unit, ss in symbols.items() for s in ss}))
                from .engine import BASE_HEADER, GP_NOTE
                header = BASE_HEADER.format(title=profile.title) + (GP_NOTE if profile.gp else "")
                with self.net.store.tx() as db:
                    pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) "
                                     "VALUES(?,?,?,?,?,?,?)", (slug, profile.title, "PS1 mapping profile", "mips", 0,
                                                              str(root), json.dumps(cfg))).lastrowid
                    self._store_functions(db, pid, functions)
                    self._store_symbols(db, pid, symbols, units)
                    db.execute("INSERT INTO context_versions(project_id,version,header,message,created_at,unit) "
                               "VALUES(?,1,?,?,?,'')", (pid, header, "mapping profile basic types", time.time()))
                    for version, unit in enumerate(units, 2):
                        db.execute("INSERT INTO context_versions(project_id,version,header,message,created_at,unit) "
                                   "VALUES(?,?,?,?,?,?)", (pid, version, header, "region basic types", time.time(), unit))
                    self.net._event(db, pid, None, "mapping", f"imported {len(units)} regions, {len(functions)} functions")
            except BaseException:
                shutil.rmtree(root)
                raise
        return dict(slug=slug, regions=len(units), functions=len(functions))

    def rescan(self, slug, image: Path, text: str, *, apply=False, log=print):
        profile = self._profile(text)
        proj = self.net._project(slug)
        cfg = self.net.cfg(proj)
        previous = cfg.get("mapping")
        if cfg.get("kind") != "rom" or not previous:
            raise NetworkError("rescan v1 requires a project created from a mapping profile; legacy imports stay unchanged")
        before_cfg = proj["config"]
        old_profile = mapping.Profile.parse(json.dumps(previous["profile"]))
        blocked = []
        if (profile.sha256, profile.format, profile.gp) != (old_profile.sha256, old_profile.format, old_profile.gp):
            blocked.append("input version, format or compiler GP context changed")
        incoming = {r.id: r for r in profile.regions}
        for r in old_profile.regions:
            other = incoming.get(r.id)
            if other is None or {k: v for k, v in r.to_dict().items() if k not in ("evidence", "confidence")} != \
                    {k: v for k, v in other.to_dict().items() if k not in ("evidence", "confidence")}:
                blocked.append(f"existing region {r.id} was removed or its target/context changed")
        with tempfile.TemporaryDirectory(prefix=".mapping-", dir=self.net.workspace) as tmp:
            prepared = self._analysis(profile, image, Path(tmp) / "analysis", log)
            old_image = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
            if hashlib.sha1(old_image).hexdigest() != cfg["rom"]["sha1"]:
                raise NetworkError("stored mapping image has changed; restore it before rescanning")
            flat, regions, added = self._layout(prepared, previous, old_image)
            functions, symbols = self._rows(prepared, regions)
            old_rows = self.net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
            expected = {(f["unit"], f["vram"]): f for f in functions}
            for old in old_rows:
                new = expected.pop((old["unit"], old["vram"]), None)
                if new is None or any(old[k] != new[k] for k in ("name", "size", "target", "relocs", "asm", "rom_offset")):
                    blocked.append(f"existing target {old['name']} differs; its history will not be overwritten")
            # New functions inside an existing region are a boundary change, not an additive region import.
            if any(f["unit"] not in added for f in expected.values()):
                blocked.append("new function boundaries inside an existing region require a new project in v1")
            derived = [r.id for r in profile.regions if r.confidence != "verified"]
            result = dict(applied=False, added_regions=added, added_functions=len(expected),
                          preserved_functions=len(old_rows), blocked=sorted(set(blocked)), derived_regions=derived)
            if not apply:
                return result
            if blocked or derived:
                raise NetworkError("mapping cannot be applied: " + "; ".join(blocked + (["derived regions are preview-only"] if derived else [])))
            revision = None
            try:
                with self.net.store.tx() as db:
                    current = db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()
                    if current["config"] != before_cfg:
                        raise NetworkError("project changed during analysis; preview the rescan again")
                    self._quiet(slug, proj["id"], db)
                    # Sources and targets are immutable here; matches, attempts, notes, and ids are never updated.
                    if added:
                        revision = self._persist(Path(proj["root"]), prepared, flat)
                        cfg["rom"]["file"] = str(revision / "image.bin")
                        cfg["rom"]["program"] = cfg["rom"]["file"]
                        cfg["rom"].update(size=len(flat), sha1=hashlib.sha1(flat).hexdigest())
                        cfg["segments"] = [dict(name=k, rom_start=r["rom_start"], rom_end=r["rom_end"], vram=r["vram"])
                                           for k, r in regions.items()]
                        cfg["mapping"].update(regions=regions, revision=str(revision))
                        self._store_functions(db, proj["id"], list(expected.values()))
                        self._store_symbols(db, proj["id"], symbols, added)
                        base = db.execute("SELECT header FROM context_versions WHERE project_id=? AND unit='' "
                                          "ORDER BY version DESC LIMIT 1", (proj["id"],)).fetchone()["header"]
                        version = db.execute("SELECT MAX(version) FROM context_versions WHERE project_id=?", (proj["id"],)).fetchone()[0]
                        for unit in added:
                            version += 1
                            db.execute("INSERT INTO context_versions(project_id,version,header,message,created_at,unit) "
                                       "VALUES(?,?,?,?,?,?)", (proj["id"], version, base, "region basic types", time.time(), unit))
                            cfg["mapping"]["symbol_units"].update({s.name: unit for s in symbols[unit]})
                    for r in profile.regions:
                        cfg["mapping"]["regions"][r.id].update(confidence=r.confidence, evidence=r.evidence)
                    cfg["mapping"]["profile"] = profile.to_dict()
                    db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
                    self.net._event(db, proj["id"], None, "mapping", f"added {len(added)} regions; preserved {len(old_rows)} functions")
            except BaseException:
                if revision is not None:
                    shutil.rmtree(Path(proj["root"]) / revision, ignore_errors=True)
                raise
            self.net._roms.pop(proj["id"], None)
            self.net._syms.clear()
            self.net._xrefs.clear()
            self.net._diffs.clear()
            self.net._ops.pop(proj["id"], None)
            self.net.twins._prints.pop(proj["id"], None)
            return result | dict(applied=True)
