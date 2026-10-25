# Installs Curator 2.0 on the laptop next to the old curator, and schedules the
# evening run. Safe to run again: it updates instead of reinstalling.
#
# One-line start (PowerShell):
#   irm https://raw.githubusercontent.com/Qrt69/newsletter-curator/v2/curator2/install.ps1 | iex

$ErrorActionPreference = 'Stop'
$Target   = 'C:\Dev\curator2'
$OldApp   = 'C:\Dev\newsletter-curator'
$Repo     = 'https://github.com/Qrt69/newsletter-curator.git'
$TaskName = 'Newsletter Curator 2.0'
$RunTime  = '20:00'

function Say($m) { Write-Host "`n>> $m" -ForegroundColor Cyan }

Say 'Benodigde programma''s nakijken'
foreach ($tool in 'git', 'uv') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        if ($tool -eq 'uv') {
            Say 'uv installeren'
            powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
            $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        } else {
            throw "git ontbreekt. Installeer Git for Windows en probeer opnieuw."
        }
    }
}

if (Test-Path "$Target\.git") {
    Say 'Bestaande installatie bijwerken'
    git -C $Target fetch origin v2
    git -C $Target checkout v2
    git -C $Target reset --hard origin/v2
} else {
    Say "Curator 2.0 ophalen naar $Target"
    git clone --branch v2 $Repo $Target
}

if (-not (Test-Path "$Target\.env")) {
    if (Test-Path "$OldApp\.env") {
        Say 'Instellingen voor Outlook overnemen van het oude programma'
        Copy-Item "$OldApp\.env" "$Target\.env"
    } else {
        throw "Geen .env gevonden in $OldApp. Zonder die gegevens kan de curator de mails niet lezen."
    }
}
foreach ($f in '.browser_state.json') {
    if ((Test-Path "$OldApp\$f") -and -not (Test-Path "$Target\$f")) { Copy-Item "$OldApp\$f" "$Target\$f" }
}

Say 'Python-pakketten installeren (eerste keer: enkele minuten)'
Push-Location $Target
uv sync
Say 'Browser voor het lezen van artikels installeren'
uv run playwright install chromium
Say 'Zelftest'
uv run python -m pytest -q curator2/tests
Pop-Location

Say "Avondrun om $RunTime inplannen"
$uv = (Get-Command uv).Source
$action  = New-ScheduledTaskAction -Execute $uv -Argument 'run python -m curator2' -WorkingDirectory $Target
$trigger = New-ScheduledTaskTrigger -Daily -At $RunTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Haalt nieuwsbrieven op, beoordeelt ze met LM Studio en schrijft naar OSK.' -Force | Out-Null

Say 'KLAAR'
Write-Host "Curator 2.0 staat in $Target en draait elke avond om $RunTime."
Write-Host "Een run nu meteen starten kan met:  Start-ScheduledTask -TaskName '$TaskName'"
