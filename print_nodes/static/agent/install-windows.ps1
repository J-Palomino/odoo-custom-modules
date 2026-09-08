<#
  Install the Mint Print Agent on Windows as a Scheduled Task (runs at logon,
  restarts on failure). Run in an elevated PowerShell:

    powershell -ExecutionPolicy Bypass -File install-windows.ps1 `
        -OdooUrl https://letsgomint.us -Token <NODE_TOKEN> [-Printer "Zebra ZD410"]

  Requires Python 3 on PATH. For PDF printing to non-Zebra printers, also
  install GhostScript (gswin64c) or SumatraPDF.
#>
param(
  [Parameter(Mandatory = $true)][string]$OdooUrl,
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$Printer = ""
)

$ErrorActionPreference = "Stop"
$dir = Join-Path $env:ProgramData "MintPrintAgent"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item (Join-Path $PSScriptRoot "mint_zebra_agent.py") $dir -Force

@"
MINT_ODOO_URL=$OdooUrl
MINT_NODE_TOKEN=$Token
MINT_AGENT_PRINTER=$Printer
MINT_POLL_SECS=2
"@ | Set-Content -Encoding ASCII (Join-Path $dir "mint_print_agent.conf")

# Prefer pythonw.exe (no console window); fall back to python.exe
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "Python 3 not found on PATH. Install Python 3 (or use the bundled mint_print_agent.exe)."; exit 1 }

$script = Join-Path $dir "mint_zebra_agent.py"
$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`"" -WorkingDirectory $dir
# Boot AND logon so it starts whether or not anyone signs in.
$trigger = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
# Battery flags are ESSENTIAL: a register that reports as "on battery" (mini-PC /
# UPS-backed / laptop) is otherwise STOPPED by Windows and never restarted
# (Windows' default is DisallowStartIfOnBatteries + StopIfGoingOnBatteries) --
# the 2026-09-08 Tempe agent drop-out. IgnoreNew avoids a second polling instance.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "MintPrintAgent" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "MintPrintAgent"

Write-Host "Mint Print Agent installed and started (Scheduled Task: MintPrintAgent)."
Write-Host "  Config: $dir\mint_print_agent.conf"
Write-Host "  Remove: Unregister-ScheduledTask -TaskName MintPrintAgent -Confirm:`$false"
Write-Host "  NOTE: PDF printing to non-Zebra printers needs GhostScript (gswin64c) or SumatraPDF installed."
