"""A release cannot silently ship mismatched package, installer or plugin versions."""
import shutil

import pytest

from tools import release
from decompnet import __version__


@pytest.fixture
def release_tree(tmp_path):
    for name in [*release.PATTERNS, "CHANGELOG.md"]:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(release.ROOT / name, destination)
    return tmp_path


def test_current_release_versions_and_notes_agree():
    assert release.check() == __version__


def test_bump_updates_all_surfaces_and_requires_completed_notes(release_tree):
    major, minor, patch = map(int, __version__.split("."))
    newer = f"{major}.{minor}.{patch + 1}"
    release.bump(newer, release_tree)
    assert set(release.versions(release_tree).values()) == {newer}
    with pytest.raises(ValueError, match="completed notes"):
        release.check(release_tree)
    changelog = release_tree / "CHANGELOG.md"
    changelog.write_text(changelog.read_text().replace("TODO: describe the changes in this release.", "A reviewed change."))
    assert release.check(release_tree) == newer
    assert "A reviewed change." in release.notes(newer, release_tree)
    with pytest.raises(ValueError, match="newer"):
        release.bump(__version__, release_tree)


def test_mismatched_installer_version_blocks_release(release_tree):
    installer = release_tree / "site/public/install.sh"
    installer.write_text(installer.read_text().replace(f'VERSION="{__version__}"', 'VERSION="0.0.0"'))
    with pytest.raises(ValueError, match="disagree"):
        release.check(release_tree)
