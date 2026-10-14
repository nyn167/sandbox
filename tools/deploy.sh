#!/usr/bin/env bash
# Mac → laptop deploy of the committed + pushed HEAD. Tests run on Windows; failure = automatic rollback.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${SANDBOX_HOST:-trader-win}"
[[ -z "$(git status --porcelain)" ]] || { echo "Uncommitted changes — commit first."; exit 1; }
git push -q origin main
TAR="$(mktemp -t sbxrel).tar"
git archive --format=tar -o "$TAR" HEAD
scp -q "$TAR" "$HOST:C:/sandbox/_release.tar"; rm -f "$TAR"
ssh "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\sandbox\\windows\\deploy.ps1" 2>&1 | grep -v "post-quantum\|store now\|openssh.com"
echo "Deployed $(git rev-parse --short HEAD)."
