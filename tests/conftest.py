import atexit
import os
import shutil
import tempfile

# the apps the tests start never look for updates on the network (tests/test_updates.py checks that part offline)
os.environ.setdefault("DECOMP_UPDATES", "off")
# and never reach the user's own app or its data: a throwaway home for the session, and a port of their own
_HOME = tempfile.mkdtemp(prefix="decomp-tests-")
os.environ["DECOMP_HOME"] = _HOME
os.environ["DECOMP_PORT"] = "16464"
atexit.register(shutil.rmtree, _HOME, True)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_surprise_downloads(monkeypatch):
    """Importing a Game Boy or Master System ROM installs the free tools it needs when they are missing: a test must
    never reach for the network. (A test of that step turns it back on and fakes the installer.)"""
    from decompnet.app.engine import RomNetwork

    monkeypatch.setattr(RomNetwork, "auto_install_tools", False)
    # The configuration probe has dedicated tests; unrelated imports should not launch background compiles.
    monkeypatch.setattr(RomNetwork, "auto_probe_compilers", False)
