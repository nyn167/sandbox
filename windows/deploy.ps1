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
# venv launcher (C:\sandbox\.venv\Scripts\python.exe) + the real interpreter it spawns (child process)
$all = Get-CimInstance Win32_Process
$launchers = $all | Where-Object { $_.ExecutablePath -like "C:\sandbox\.venv\*" -and ($_.CommandLine -match "run\.py|bots\.py") }
$ids = @($launchers.ProcessId) + @(($all | Where-Object { $launchers.ProcessId -contains $_.ParentProcessId }).ProcessId)
$ids | Where-Object { $_ } | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Write-Host "Stopped $(@($ids | Where-Object { $_ }).Count) sandbox process(es)."
Write-Host "Deployed. Server restarts within ~30 s, bots within ~60 s."
