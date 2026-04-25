# One-shot helper to push this repo to GitHub.
#
# Prereqs on your Windows machine:
#   1. Git for Windows installed: https://git-scm.com/download/win
#   2. EITHER the GitHub CLI (recommended): https://cli.github.com
#      OR a GitHub account + you've created an empty repo on github.com
#
# Usage:
#   .\push-to-github.ps1 -RepoName autotune -Private
#   .\push-to-github.ps1 -RepoName autotune-oc -Public
#
# With -UseGhCli (the default when gh.exe is found), this will:
#   1. git init
#   2. git add + commit
#   3. gh auth login (if you're not logged in) -- opens a browser
#   4. gh repo create elijah/autotune --source=. --push
#
# Without gh, it stops after the commit and prints what to run next.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoName,

    [switch]$Private,
    [switch]$Public,
    [switch]$NoPush
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not $Private -and -not $Public) {
    $Private = $true      # default to private
}
$visibility = if ($Private) { '--private' } else { '--public' }

# --- sanity check prereqs ---
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not installed. Get it from https://git-scm.com/download/win"
}
$hasGh = [bool](Get-Command gh -ErrorAction SilentlyContinue)

# --- git init + commit ---
if (-not (Test-Path .git)) {
    Write-Host "==> git init" -ForegroundColor Cyan
    git init -b main | Out-Null
}

# Make sure the user's identity is set for the commit.
$email = git config user.email 2>$null
$name  = git config user.name  2>$null
if (-not $email) {
    $email = Read-Host "Your git commit email (e.g. you@example.com)"
    git config user.email $email
}
if (-not $name) {
    $name = Read-Host "Your git commit name (e.g. Elijah)"
    git config user.name $name
}

Write-Host "==> staging files" -ForegroundColor Cyan
git add -A

$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host "Nothing to commit -- already clean." -ForegroundColor Yellow
} else {
    Write-Host "Files that will be committed:"
    $staged | Select-Object -First 30 | ForEach-Object { "    $_" }
    if ($staged.Count -gt 30) { Write-Host "    ... and $($staged.Count - 30) more" }

    Write-Host "==> git commit" -ForegroundColor Cyan
    git commit -m "Initial import: autotune Intel K-SKU overclocker" | Out-Null
}

if ($NoPush) {
    Write-Host "`nCommit made. -NoPush set, stopping here." -ForegroundColor Green
    return
}

# --- push path 1: gh cli ---
if ($hasGh) {
    Write-Host "==> gh auth status" -ForegroundColor Cyan
    $ghStatus = gh auth status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Not logged in to GitHub. Launching browser login..." -ForegroundColor Yellow
        gh auth login
        if ($LASTEXITCODE -ne 0) { throw "gh auth login failed" }
    }

    Write-Host "==> creating repo on github.com and pushing" -ForegroundColor Cyan
    gh repo create $RepoName $visibility --source=. --push --remote=origin
    if ($LASTEXITCODE -ne 0) { throw "gh repo create failed" }

    $url = gh repo view --json url --jq .url 2>$null
    if ($url) {
        Write-Host "`nDone. View at: $url" -ForegroundColor Green
    }
    return
}

# --- push path 2: manual ---
Write-Host @"

gh CLI not found. Finish the push manually:

1. Go to https://github.com/new and create an EMPTY repo named '$RepoName'
   (no README, no .gitignore, no license -- this repo already has those).

2. Back here, run (replace YOUR_USERNAME with your GitHub username):

    git remote add origin https://github.com/YOUR_USERNAME/$RepoName.git
    git push -u origin main

If push asks for credentials, GitHub wants a Personal Access Token, not a
password. Create one at https://github.com/settings/tokens (scope: repo).

"@ -ForegroundColor Yellow
