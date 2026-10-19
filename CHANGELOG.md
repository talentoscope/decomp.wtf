# Changelog

## 0.10.7

- Use SynapticDreams/decomp.wtf for installers, source links and updates.
- Discover stable versions from published GitHub releases; install their wheel or tagged source.
- Migrate archived-repository install metadata while preserving explicitly selected custom forks.
- Reinstall persistent container packages when the bundled installer version changes, unless an explicit installation marker is set.
- Keep package, installer and plugin versions together with a release check and version-bump command.
- Publish versioned release assets after a version bump is merged into main.
- Preserve repeated section ranges and linker priorities when editing dtk splits.
