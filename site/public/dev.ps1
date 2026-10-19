# decomp.wtf's development branch, for testing what isn't released yet (Windows, PowerShell):
#
#   irm https://raw.githubusercontent.com/SynapticDreams/decomp.wtf/main/site/public/dev.ps1 | iex
#
# Runs main's installer by default, or the branch selected with DECOMP_BRANCH. decomp.wtf installs from the
# branch's newest commit, and the app then offers every new commit pushed there as an update (one click).
# Back to releases: irm https://github.com/SynapticDreams/decomp.wtf/releases/latest/download/install.ps1 | iex
& {
$branch = if ($env:DECOMP_BRANCH) { $env:DECOMP_BRANCH } else { "main" }
$had = $env:DECOMP_BRANCH
$env:DECOMP_BRANCH = $branch
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $script = Invoke-RestMethod -UseBasicParsing -UserAgent "decomp.wtf-installer (PowerShell)" "https://raw.githubusercontent.com/SynapticDreams/decomp.wtf/$branch/site/public/install.ps1"
    Invoke-Expression $script
} finally {
    $env:DECOMP_BRANCH = $had
}
}
