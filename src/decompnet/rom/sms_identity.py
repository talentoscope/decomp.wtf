"""Revision identity for SMS/GG inputs, independent of mapper guesses and project storage."""
from __future__ import annotations

import hashlib

VERSION = 1


def strip_copier_header(data: bytes) -> bytes:
    """Normalize only a valid-sized image with a recognized header after the copier prefix."""
    size = len(data) - 512
    if 0x2000 <= size <= 0x400000 and size % 0x2000 == 0:
        at = 0x7FF0 if size >= 0x8000 else 0x3FF0 if size >= 0x4000 else 0x1FF0
        if data[512 + at:512 + at + 8] == b"TMR SEGA":
            return data[512:]
    return data


def verify_normalized(rom: bytes, identity: dict | None, legacy_sha1: str | None = None) -> None:
    """Refuse a replacement comparison target before generating or deleting export artifacts."""
    if identity is not None:
        if type(identity.get("version")) is not int or identity["version"] != VERSION:
            raise ValueError("unsupported SMS identity version")
        actual = {"sha256": hashlib.sha256(rom).hexdigest(), "size": len(rom)}
        if identity.get("normalized") != actual:
            raise ValueError("ROM differs from the project's frozen normalized identity")
    if legacy_sha1 and hashlib.sha1(rom).hexdigest() != legacy_sha1:
        raise ValueError("ROM differs from the project's frozen SHA-1 identity")


def identify(raw: bytes, normalized: bytes, info: dict) -> dict:
    """Retain the supplied dump identity while binding analysis to normalized ROM bytes."""
    removed = len(raw) - len(normalized)
    if removed not in (0, 512) or raw[removed:] != normalized:
        raise ValueError("SMS identity requires unchanged bytes or a removed 512-byte copier header")
    return {
        "version": VERSION,
        "platform": info["platform"],
        "platform_origin": "header" if info.get("header") else "filename",
        "product_code": info.get("game_code") or None,
        "revision": info.get("version") if info.get("header") else None,
        "normalized": {"sha256": hashlib.sha256(normalized).hexdigest(), "size": len(normalized)},
        "inputs": [{"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
                    "copier_header_size": removed}],
    }


def merge_inputs(previous: dict | None, incoming: dict) -> dict:
    """An alternate dump wrapper is evidence for the same revision, not a new comparison target."""
    if type(incoming.get("version")) is not int or incoming["version"] != VERSION or \
            (previous and (type(previous.get("version")) is not int or previous["version"] != VERSION)):
        raise ValueError("unsupported SMS identity version")
    if previous is None:
        return incoming
    if previous.get("normalized") != incoming.get("normalized") or previous.get("platform") != incoming.get("platform"):
        raise ValueError("SMS identities refer to different normalized revisions or platforms")
    inputs = {entry["sha256"]: entry for entry in previous["inputs"]}
    for entry in incoming["inputs"]:
        inputs.setdefault(entry["sha256"], entry)
    if len(inputs) > 64:
        raise ValueError("too many alternate input identities for this revision")
    return {**previous, "inputs": sorted(inputs.values(), key=lambda entry: entry["sha256"])}
