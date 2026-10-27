# Installs Curator 2.0 on the laptop next to the old curator, and schedules the
# evening run. Safe to run again: it updates instead of reinstalling.
#
# One-line start (PowerShell):
#   irm https://raw.githubusercontent.com/Qrt69/newsletter-curator/v2/curator2/install.ps1 | iex

$ErrorActionPreference = 'Stop'
$Target   = 'C:\Dev\curator2'
$Config   = 'C:\Dev\curator2-instellingen'
$OldApp   = 'C:\Dev\newsletter-curator'
$Repo     = 'https://github.com/Qrt69/newsletter-curator.git'
$TaskName = 'Newsletter Curator 2.0'
$RunTime  = '20:00'

function Say($m) { Write-Host "`n>> $m" -ForegroundColor Cyan }
# Windows PowerShell does not stop on a failing external program by itself.
function Run([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "Mislukt: $what (foutcode $LASTEXITCODE). Er is niets ingepland." }
}

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
    git -C $Target fetch origin v2;        Run 'nieuwe versie ophalen'
    git -C $Target checkout v2;            Run 'naar versie 2 overschakelen'
    git -C $Target reset --hard origin/v2; Run 'bijwerken' 
} else {
    Say "Curator 2.0 ophalen naar $Target"
    git clone --branch v2 $Repo $Target;   Run 'programma ophalen' 
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

# Kurt's settings live outside the program folder, so an update never overwrites them.
if (-not (Test-Path "$Config\instellingen.toml")) {
    Say "Instellingen klaarzetten in $Config (hier pas je ze later aan)"
    New-Item -ItemType Directory -Force -Path $Config | Out-Null
    Copy-Item "$Target\curator2\config\*.toml" $Config
}

# New settings blocks from an update are added to Kurt's own file, never replacing his values.
$own = Get-Content "$Config\instellingen.toml" -Raw -Encoding UTF8
$def = Get-Content "$Target\curator2\config\instellingen.toml" -Raw -Encoding UTF8
foreach ($block in [regex]::Matches($def, '(?ms)^\[(\w+)\]\s*$.*?(?=^\[|\z)')) {
    $name = $block.Groups[1].Value
    if ($own -notmatch "(?m)^\[$name\]") {
        Say "Nieuwe instellingen [$name] toevoegen aan $Config\instellingen.toml"
        Add-Content -Path "$Config\instellingen.toml" -Value ("`r`n" + $block.Value.TrimEnd()) -Encoding UTF8
    }
}

Say 'Python-pakketten installeren (eerste keer: enkele minuten)'
Push-Location $Target
try {
    uv sync;                            Run 'Python-pakketten installeren'
    Say 'Browser voor het lezen van artikels installeren'
    uv run playwright install chromium; Run 'browser installeren'
    Say 'Zelftest'
    uv run python -m pytest -q curator2/tests; Run 'zelftest'
} finally { Pop-Location }

Say "Avondrun om $RunTime inplannen"
$uv = (Get-Command uv).Source
$action  = New-ScheduledTaskAction -Execute $uv -Argument 'run python -m curator2' -WorkingDirectory $Target
$trigger = New-ScheduledTaskTrigger -Daily -At $RunTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Haalt nieuwsbrieven op, laat ze beoordelen en schrijft naar OSK.' -Force | Out-Null

Say 'KLAAR'
Write-Host "Curator 2.0 staat in $Target en draait elke avond om $RunTime."
Write-Host "Je instellingen staan in $Config."
Write-Host "Een run nu meteen starten kan met:  Start-ScheduledTask -TaskName '$TaskName'"
