#!/usr/bin/env bash
# decomp.wtf's development branch, for testing what isn't released yet (Linux):
#
#   curl -fsSL https://raw.githubusercontent.com/SynapticDreams/decomp.wtf/main/site/public/dev.sh | bash
#
# Runs main's installer by default, or the branch selected with DECOMP_BRANCH. decomp.wtf installs from the
# branch's newest commit, and the app then offers every new commit pushed there as an update (one click).
# Back to releases: curl -fsSL https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.sh | bash
set -euo pipefail
BRANCH="${DECOMP_BRANCH:-main}"
curl -fsSL "https://raw.githubusercontent.com/SynapticDreams/decomp.wtf/$BRANCH/site/public/install.sh" | DECOMP_BRANCH="$BRANCH" bash -s -- "$@"
