"""The settings file (API endpoints and keys, budget, helpers to resume) is written whole or not at all, and one that
can't be read is kept aside, not replaced by empty settings on the next save."""

import json
import threading

from decompnet.app.server import Settings


def test_saves_at_once_never_garble_the_file(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)

    def save(n):
        for i in range(40):
            s.data[f"k{n}"] = "x" * (i * 50)
            s.save()
    threads = [threading.Thread(target=save, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(json.loads(path.read_text())) >= {f"k{n}" for n in range(6)}
    assert oct(path.stat().st_mode & 0o777) == "0o600"  # it holds API keys
    assert not list(tmp_path.glob(".*.tmp"))


def test_a_garbled_file_is_kept_aside(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"endpoints": [{"id": "e1", "api_key": "sk-')
    s = Settings(path)
    assert s.data.get("endpoints") == []
    kept = list(tmp_path.glob("settings.json.broken-*"))
    assert len(kept) == 1 and "sk-" in kept[0].read_text()
    s.save()
    assert json.loads(path.read_text())["endpoints"] == [] and kept[0].exists()
