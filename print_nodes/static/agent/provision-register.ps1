<#
  Provision a Mint store register for receipt printing, end to end, from one
  command. Idempotent - safe to re-run. Run in an elevated PowerShell:

    powershell -ExecutionPolicy Bypass -File provision-register.ps1 `
        -OdooUrl https://letsgomint.us -Token <NODE_TOKEN>

  It:
    1. installs Python 3 (silent) and SumatraPDF (portable) if missing
    2. installs the bundled Star TSP100/TSP143 x64 (futurePRNT) print driver
    3. creates the "Star TSP143" print queue on the printer's USB port
    4. applies the cash-drawer driver config (fire drawer at document bottom)
    5. installs + starts the print agent as a boot service, pointed at this
       store's node token; the node then registers itself in Odoo

  Everything it needs is fetched from this same agent/ folder on the Odoo host
  (OdooUrl/print_nodes/static/agent/...), so a store tech only needs this file,
  the URL, and the node token (Point of Sale -> PrintNodes -> Print Nodes).

  The Star TSP100/TSP143 futurePRNT is raster-only, so receipts are rendered to
  PDF by Odoo and printed through the OS driver (that is why SumatraPDF is
  needed). The agent auto-registers the queue and Odoo classifies it as a PDF
  receipt printer automatically.
#>
param(
  [Parameter(Mandatory = $true)][string]$OdooUrl,
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$PrinterName = 'Star TSP143'
)

$ErrorActionPreference = 'Continue'
$base = ($OdooUrl.TrimEnd('/')) + '/print_nodes/static/agent'
$dir = Join-Path $env:ProgramData 'MintPrintAgent'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir 'provision.log'
function Log($m) { $line = "$(Get-Date -Format o)  $m"; Write-Host $line; $line | Out-File $log -Append -Encoding utf8 }
function Fetch($rel, $dest) { Invoke-WebRequest -Uri "$base/$rel" -OutFile $dest -UseBasicParsing -TimeoutSec 180 }
Log "=== provision start (Odoo=$OdooUrl printer=$PrinterName) ==="

# ---- 1a. Python 3 -----------------------------------------------------------
if (Get-Command python -ErrorAction SilentlyContinue) {
  Log 'python: already present'
} else {
  try {
    $pyi = Join-Path $env:TEMP 'python-setup.exe'
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile $pyi -UseBasicParsing -TimeoutSec 300
    Start-Process $pyi -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_test=0','Include_launcher=1' -Wait
    Remove-Item $pyi -Force -ErrorAction SilentlyContinue
    Log 'python: installed'
  } catch { Log "python FAIL: $($_.Exception.Message)" }
}
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-ChildItem 'C:\Program Files\Python3*\pythonw.exe' -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }

# ---- 1b. SumatraPDF (renders receipt PDFs to the raster driver) -------------
if (Test-Path "$dir\SumatraPDF.exe") {
  Log 'sumatra: already present'
} else {
  try {
    $zip = Join-Path $env:TEMP 'sumatra.zip'
    Invoke-WebRequest -Uri 'https://www.sumatrapdfreader.org/dl/rel/3.5.2/SumatraPDF-3.5.2-64.zip' -OutFile $zip -UseBasicParsing -TimeoutSec 180
    Expand-Archive -Path $zip -DestinationPath "$dir\sumatra" -Force
    $exe = Get-ChildItem "$dir\sumatra" -Filter 'SumatraPDF*.exe' -Recurse | Select-Object -First 1
    if ($exe) { Copy-Item $exe.FullName "$dir\SumatraPDF.exe" -Force; Log 'sumatra: installed' }
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
  } catch { Log "sumatra FAIL: $($_.Exception.Message)" }
}

# ---- 2. Star x64 futurePRNT driver -----------------------------------------
if (Get-PrinterDriver -Name 'Star TSP100 Cutter (TSP143)' -ErrorAction SilentlyContinue) {
  Log 'driver: already registered'
} else {
  try {
    $dz = Join-Path $dir 'startsp_x64.zip'
    Fetch 'drivers/startsp_x64.zip' $dz
    $ex = Join-Path $dir 'startsp64'
    Remove-Item $ex -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $dz -DestinationPath $ex -Force
    $inf = Get-ChildItem $ex -Filter 'smjt100.inf' -Recurse | Select-Object -First 1
    if ($inf) {
      & pnputil.exe /add-driver $inf.FullName /install | Out-Null
      # register the driver with the spooler so Add-Printer can use it
      try { Add-PrinterDriver -Name 'Star TSP100 Cutter (TSP143)' -ErrorAction Stop } catch {}
      Log "driver: installed from $($inf.Name)"
    } else { Log 'driver: smjt100.inf not found in bundle' }
  } catch { Log "driver FAIL: $($_.Exception.Message)" }
}

# ---- 3. print queue on the printer's USB port ------------------------------
if (Get-Printer -Name $PrinterName -ErrorAction SilentlyContinue) {
  Log "queue: '$PrinterName' already exists"
} else {
  # find the USB printing port the Star enumerated on (USBPRINT\... -> USBnnn)
  $port = (Get-PrinterPort | Where-Object { $_.Name -match '^USB\d' } | Select-Object -First 1).Name
  if (-not $port) { $port = 'USB001' }
  try {
    Add-Printer -Name $PrinterName -DriverName 'Star TSP100 Cutter (TSP143)' -PortName $port -ErrorAction Stop
    Log "queue: created '$PrinterName' on $port"
  } catch { Log "queue FAIL: $($_.Exception.Message)" }
}

# ---- 4. cash-drawer config (fire drawer at document bottom) -----------------
# Replays the driver DEVMODE captured from a known-good register. If it does not
# take (driver blob not portable to this build), the message below tells the
# tech the 4-click manual step.
try {
  $cfg = Join-Path $dir 'star_drawer.json'
  Fetch 'drivers/star_drawer.json' $cfg
  $d = Get-Content $cfg -Raw | ConvertFrom-Json
  $k = "HKLM:\SYSTEM\CurrentControlSet\Control\Print\Printers\$PrinterName\PrinterDriverData"
  if ((Test-Path $k) -and $d.PrinterData) {
    Set-ItemProperty -Path $k -Name 'PrinterData' -Value ([Convert]::FromBase64String($d.PrinterData)) -Type Binary
    if ($d.FeatureKeyword) { Set-ItemProperty -Path $k -Name 'FeatureKeyword' -Value ([Convert]::FromBase64String($d.FeatureKeyword)) -Type Binary }
    Restart-Service Spooler -Force
    Start-Sleep -Seconds 3
    $fk = (Get-ItemProperty $k -Name FeatureKeyword -ErrorAction SilentlyContinue).FeatureKeyword
    $txt = -join ($fk | ForEach-Object { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { ' ' } })
    if ($txt -match 'CashDrawer' -and $txt -match 'Document') {
      Log 'drawer: config applied (Cash Drawer, document bottom)'
    } else {
      Log 'drawer: MANUAL STEP NEEDED - Printer properties > Device Settings > Peripheral Unit Type=Cash Drawer, Peripheral Unit 1=Document Bottom'
    }
  }
} catch { Log "drawer note: $($_.Exception.Message) - set it manually in Printer properties > Device Settings" }

# ---- 5. print agent (boot service, this store's node) ----------------------
try {
  Fetch 'mint_zebra_agent.py' "$dir\mint_zebra_agent.py"
  @"
MINT_ODOO_URL=$OdooUrl
MINT_NODE_TOKEN=$Token
MINT_AGENT_PRINTER=$PrinterName
MINT_POLL_SECS=2
"@ | Set-Content -Encoding ascii "$dir\mint_print_agent.conf"
  if (-not $py) { Log 'agent: python missing, cannot start'; }
  else {
    $action = New-ScheduledTaskAction -Execute $py -Argument ('"' + "$dir\mint_zebra_agent.py" + '"') -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName 'MintPrintAgent' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*Python3*' } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName 'MintPrintAgent'
    Log 'agent: installed and started (Scheduled Task MintPrintAgent, at boot as SYSTEM)'
  }
} catch { Log "agent FAIL: $($_.Exception.Message)" }

Log '=== provision done ==='
Write-Host ""
Write-Host "Done. In Odoo (Point of Sale > PrintNodes > Print Nodes) the node should show Online and '$PrinterName' as a PDF receipt printer within ~1 min."
Write-Host "If it is not the default receipt printer, tick 'Default for role' on it."
