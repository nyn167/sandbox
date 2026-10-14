# Called by tools/deploy.sh on the Mac after it uploaded C:\sandbox\_release.tar (git archive of HEAD).
# Keeps the previous code, runs the tests on Windows, rolls back on failure, then restarts server + bots.
$ErrorActionPreference = "Stop"
Set-Location C:\sandbox
$keep = "_prev.tar"
tar -cf $keep --exclude .venv --exclude data --exclude .env --exclude _release.tar --exclude _prev.tar .
tar -xf _release.tar
Remove-Item _release.tar
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
$p = Start-Process -NoNewWindow -PassThru -Wait -FilePath .venv\Scripts\python.exe -ArgumentList "-m","pytest","-q","-p","no:cacheprovider" -RedirectStandardOutput _t.out -RedirectStandardError _t.err
Get-Content _t.out -Tail 3
if ($p.ExitCode -ne 0) {
    Get-Content _t.out -Tail 30
    tar -xf $keep
    Write-Host "TESTS FAILED on Windows - previous code restored, services not restarted."
    Remove-Item _t.out,_t.err
    exit 1
}
Remove-Item _t.out,_t.err
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*C:\sandbox\.venv*" -and ($_.CommandLine -like "*run.py*" -or $_.CommandLine -like "*bots.py*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Deployed. Server restarts within ~30 s, bots within ~60 s."
