#!/usr/bin/env bash
# Mac → Windows laptop (C:\sandbox): copy the source tree, then optionally run a command there.
#   tools/sync.sh                 copy only
#   tools/sync.sh test            copy + pytest on the laptop (low CPU priority)
#   tools/sync.sh pip             copy + pip install -r requirements.txt on the laptop
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${SANDBOX_HOST:-trader-win}"
TAR="$(mktemp -t sbx).tar"
COPYFILE_DISABLE=1 tar --exclude ._* --exclude .venv --exclude data/db --exclude data/logs --exclude .env --exclude __pycache__ --exclude .pytest_cache \
    --exclude .git -cf "$TAR" .
scp -q "$TAR" "$HOST:C:/sandbox/_sync.tar"
rm -f "$TAR"
ssh "$HOST" 'cd C:\sandbox; tar -xf _sync.tar; Remove-Item _sync.tar' 2>&1 | grep -v "post-quantum\|store now\|openssh.com" || true
case "${1:-}" in
  test) ssh "$HOST" 'cd C:\sandbox; $p = Start-Process -NoNewWindow -PassThru -Wait -FilePath .venv\Scripts\python.exe -ArgumentList "-m","pytest","-q","-p","no:cacheprovider" -RedirectStandardOutput _t.out -RedirectStandardError _t.err; Get-Content _t.out -Tail 25; Get-Content _t.err -Tail 15; Remove-Item _t.out,_t.err' 2>&1 | grep -v "post-quantum\|store now\|openssh.com" ;;
  pip)  ssh "$HOST" 'cd C:\sandbox; .venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt; "pip done"' 2>&1 | grep -v "post-quantum\|store now\|openssh.com" ;;
esac
