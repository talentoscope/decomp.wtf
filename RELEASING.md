# Releases

Installers and update checks use `SynapticDreams/decomp.wtf`. Stable installations
follow published GitHub releases. Branch installations follow commits on their
selected branch. Existing metadata pointing at the archived repository is
normalized to this fork after installing this version.

For a new release:

1. Run `uv run python tools/release.py bump 0.10.8`, choosing the next version.
   This updates the package, lockfile, runtime, both installers and both plugin
   manifests together.
2. Complete the new `CHANGELOG.md` entry. The release check rejects unfinished notes.
3. Run `uv run python tools/release.py check` and the relevant tests, then merge the
   reviewed change into `main`.

The Release workflow checks versions, tests the updater/installers, builds the
wheel, creates a draft `vMAJOR.MINOR.PATCH` release, uploads all installer assets,
and publishes it. Only then can the stable updater offer it. An already published
version is never replaced; use another version bump for subsequent changes. An
interrupted draft can be completed by rerunning the workflow. The workflow also
has a manual Run workflow action on `main`.

The changelog supplies release notes. The wheel, installers, shared setup script
and icon are all versioned together. Source-only releases remain installable from
their tag when a wheel is unavailable.

Fresh installation:

```bash
curl -fsSL https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.sh | bash
```

```powershell
irm https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.ps1 | iex
```

An existing installation of the old updater needs one reinstall from this fork
before it can discover fork releases. Running from a checkout installs that
checkout and records its branch; `--source release` selects stable releases.

Persistent container installations use the bundled installer's version as their
default installation marker. Rebuilding and recreating with a newer bundle
therefore reinstalls the app while retaining the data volume. An explicit
`DECOMP_INSTALL_ID` overrides that behavior; change it when requesting a reinstall.
The app's updater continues to respect its existing idle/confirmation settings.
