# Install or update the context-handoff hooks, orch, its MCP server and its scheduled tasks on this
# Windows machine: the same steps as install.sh, which hands over to this file under Git Bash.
#   powershell -ExecutionPolicy Bypass -File install.ps1
if ($IsLinux -or $IsMacOS) { bash "$PSScriptRoot/install.sh"; exit $LASTEXITCODE }
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = (& python -c "import sys; assert sys.version_info >= (3, 8); print(sys.executable)").Trim()
if (-not $python) { throw 'Python 3.8 or newer must be on PATH as python' }
$hooks = Join-Path $HOME '.claude\hooks'
$bin = Join-Path $HOME '.local\bin'
New-Item -ItemType Directory -Force $hooks, $bin | Out-Null
Copy-Item hooks\*.py $hooks -Force

# orch for PowerShell and cmd (orch.cmd) and for Git Bash, the shell of Claude's Bash tool (orch).
# WriteAllText: no BOM, and sh needs its LF.
$orch = Join-Path $hooks 'orch.py'
[IO.File]::WriteAllText("$bin\orch.cmd", "@`"$python`" -X utf8 `"$orch`" %*`r`n")
[IO.File]::WriteAllText("$bin\orch", "#!/bin/sh`nexec `"$($python -replace '\\', '/')`" -X utf8 `"$($orch -replace '\\', '/')`" `"`$@`"`n")
if (($env:PATH -split ';') -notcontains $bin) { Write-Warning "$bin is not on PATH: orch won't be found" }

& $python -X utf8 (Join-Path $hooks 'context-handoff-install.py')
if ($LASTEXITCODE) { throw 'merging the hooks into settings.json failed' }

# The orch MCP server, at user scope in every account (each keeps its own .claude.json).
if (Get-Command claude -ErrorAction SilentlyContinue) {
    $saved = $env:CLAUDE_CONFIG_DIR
    $profiles = @('') + (Get-ChildItem (Join-Path $HOME '.claude-accounts\acc*') -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName '.claude.json') } | ForEach-Object FullName)
    foreach ($profile_dir in $profiles) {
        $env:CLAUDE_CONFIG_DIR = $profile_dir  # empty unsets it: account 1
        cmd /c "claude mcp get orch >nul 2>&1"
        if ($LASTEXITCODE) { claude mcp add --scope user orch -- $python -X utf8 (Join-Path $hooks 'orch_mcp.py') }
    }
    $env:CLAUDE_CONFIG_DIR = $saved
} else {
    Write-Warning "claude not on PATH: register the MCP server with 'claude mcp add --scope user orch -- $python -X utf8 $hooks\orch_mcp.py'"
}

# conhost --headless: a hidden console for python and every herdr it calls, so nothing flashes on
# screen. The tasks run only while logged on, which is when the herdr server runs.
function Register-OrchTask($name, $description, $command, $trigger, $settings) {
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' -WorkingDirectory $HOME `
        -Argument "--headless `"$python`" -X utf8 `"$orch`" $command"
    Register-ScheduledTask -TaskName $name -Description $description -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
}
$common = @{ AllowStartIfOnBatteries = $true; DontStopIfGoingOnBatteries = $true
             StartWhenAvailable = $true; MultipleInstances = 'IgnoreNew' }
Register-OrchTask 'orch-watchdog' 'orch watchdog: wake idle herdr orchestrators whose workers sit idle' 'watchdog' `
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)) `
    (New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit (New-TimeSpan -Minutes 5))

# The gateway starts at logon but not now: it needs `orch pair`, then Start-ScheduledTask orch-serve.
# Before that `orch serve` exits 3 and stays stopped. A running one is restarted onto the new code.
$wasRunning = (Get-ScheduledTask orch-serve -ErrorAction SilentlyContinue).State -eq 'Running'
if ($wasRunning) { Stop-ScheduledTask orch-serve }
Register-OrchTask 'orch-serve' "orch serve: the gateway for the owner's phone app, on the Tailscale IP only" 'serve' `
    (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
    (New-ScheduledTaskSettingsSet @common -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1))
if ($wasRunning) { Start-ScheduledTask orch-serve }
