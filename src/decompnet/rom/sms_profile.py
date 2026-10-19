"""Declared cartridge mapping and build intent, separate from ROM identity and analysis results."""
from __future__ import annotations

import re

from .sms_identity import VERSION as IDENTITY_VERSION

VERSION = 1
MAPPERS = ("unknown", "sega", "mapperless", "codemasters", "unsupported")
VARIANTS = ("exact-original", "annotated-original", "experimental")
SUPPORTED = ("sega",)
FIELDS = {"version", "mapper", "variant", "origin", "reason", "sha256", "size"}


def _normalized(rom):
    identity = rom.get("identity")
    if identity is None:
        return {}
    if not isinstance(identity, dict) or type(identity.get("version")) is not int or identity["version"] != IDENTITY_VERSION:
        raise ValueError("unsupported SMS/GG ROM identity version")
    normalized = identity.get("normalized")
    if not isinstance(normalized, dict) or not isinstance(normalized.get("sha256"), str) or \
            not re.fullmatch(r"[0-9a-f]{64}", normalized["sha256"]) or normalized.get("size") != rom.get("size"):
        raise ValueError("invalid SMS/GG normalized ROM identity")
    return normalized


def initial(rom: dict, origin: str = "unresolved") -> dict:
    normalized = _normalized(rom)
    return {"version": VERSION, "mapper": "unknown", "variant": "annotated-original", "origin": origin,
            "reason": "", "sha256": normalized.get("sha256"), "size": rom.get("size", 0)}


def read(cfg: dict) -> dict:
    rom = cfg.get("rom") or {}
    if not isinstance(rom, dict):
        raise ValueError("invalid SMS/GG ROM metadata")
    raw = cfg.get("sms_profile")
    if raw is None:
        return initial(rom, "legacy")
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ValueError("invalid SMS/GG profile fields")
    if type(raw["version"]) is not int or raw["version"] != VERSION:
        raise ValueError("unsupported SMS/GG profile version")
    if raw["mapper"] not in MAPPERS or raw["variant"] not in VARIANTS:
        raise ValueError("unknown SMS/GG mapper or build variant")
    if raw["origin"] not in ("unresolved", "legacy", "operator") or not isinstance(raw["reason"], str) or len(raw["reason"]) > 1000:
        raise ValueError("invalid SMS/GG profile origin or reason")
    if raw["origin"] == "operator" and not 20 <= len(raw["reason"]) <= 1000:
        raise ValueError("explain the mapping evidence in 20 to 1000 characters")
    if raw["mapper"] != "unknown" and raw["origin"] != "operator":
        raise ValueError("a declared SMS/GG mapper needs operator evidence")
    digest = raw["sha256"]
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("invalid SMS/GG profile ROM identity")
    normalized = _normalized(rom)
    if raw["origin"] == "operator" and (digest is None or not normalized):
        raise ValueError("a declared SMS/GG profile needs a frozen normalized ROM identity")
    if normalized and digest != normalized.get("sha256"):
        raise ValueError("SMS/GG profile belongs to another revision")
    if type(raw["size"]) is not int or raw["size"] != rom.get("size"):
        raise ValueError("SMS/GG profile ROM size differs")
    return dict(raw)


def describe(cfg: dict) -> dict | None:
    if cfg.get("platform") not in ("sms", "gg") and cfg.get("arch") != "z80":
        return None
    try:
        profile = read(cfg)
    except (ValueError, TypeError) as exc:
        return {"status": "invalid", "supported": False, "message": str(exc), "profile": None}
    mapper = profile["mapper"]
    # Additional models become supported only when the shared mapper implementation is integrated.
    supported = mapper in SUPPORTED and profile["origin"] == "operator"
    status = "declared" if supported else "legacy" if profile["origin"] == "legacy" else \
        "unresolved" if mapper == "unknown" else "unsupported"
    message = "Cartridge mapping declared; static analysis still has the limits described in the SMS guide." if supported else \
        "Declare the cartridge mapping in project Settings before accepting new source." if mapper == "unknown" else \
        "This cartridge mapping is recorded but its analysis model is not supported yet."
    return {"status": status, "supported": supported, "profile": profile, "message": message,
            "discovery": "declared-sega-model" if supported else "provisional-sega-listing",
            "exact_original_intent": profile["variant"] in ("exact-original", "annotated-original"),
            "supported_mappers": list(SUPPORTED)}


def require_supported(cfg: dict) -> None:
    if cfg.get("kind") != "rom":
        return
    view = describe(cfg)
    if view is not None and not view["supported"]:
        raise ValueError(view["message"])
