"""Offline mapping commands; refuse to share a database with a running server."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..network import NetworkError
from ..rom.mapping import MappingError, Profile, prepare


def run(args):
    import tempfile
    from .cli import _running_version
    from .engine import RomNetwork

    try:
        if args.action == "discover":
            from ..rom.overlay_discovery import discover

            result = discover(args.image)
            profile = result.pop("profile")
            if profile:
                with args.profile.open("x", encoding="utf-8") as output:
                    output.write(json.dumps(profile, indent=2) + "\n")
            print(json.dumps(result, indent=2))
            return 0
        if args.profile.stat().st_size > 1024 * 1024:
            raise MappingError("profile exceeds 1 MiB")
        text = args.profile.read_text(encoding="utf-8")
        profile = Profile.parse(text)
        if args.action == "validate":
            with tempfile.TemporaryDirectory(prefix="decomp-map-") as tmp:
                result = prepare(profile, args.image, Path(tmp) / "analysis", lambda s: print(s, file=sys.stderr))
                print(json.dumps(dict(valid=True, derived_regions=[r.id for r in profile.regions if r.confidence != "verified"],
                                      functions={k: len(v) for k, v in result.functions.items()}), indent=2))
            return 0
        if args.workspace is None:
            raise MappingError("pass --workspace for offline create/preview/apply; use the API when the app is running")
        workspace = args.workspace.expanduser().resolve()
        state = workspace / ".decomp/server.json"
        if state.exists():
            previous = json.loads(state.read_text(encoding="utf-8"))
            if _running_version(previous.get("port") or 0) is not None:
                raise MappingError("this workspace's server is running; use /api/mapping instead of offline commands")
        internal = workspace / ".decomp"
        internal.mkdir(parents=True, exist_ok=True)
        net = RomNetwork(internal / "decomp.db", workspace)
        if args.action == "create":
            result = net.mapping.create(args.image, text, confirmed=args.own_copy,
                                        log=lambda s: print(s, file=sys.stderr))
        else:
            if not args.project:
                raise MappingError("pass --project SLUG for preview/apply")
            result = net.mapping.rescan(args.project, args.image, text, apply=args.action == "apply",
                                        log=lambda s: print(s, file=sys.stderr))
        print(json.dumps(result, indent=2))
        return 0
    except (MappingError, NetworkError, OSError, ValueError) as e:
        print(f"mapping: {e}", file=sys.stderr)
        return 1
