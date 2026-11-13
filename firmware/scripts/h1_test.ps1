# h1_test.ps1 - forwards the USB-IDE adapter to WSL via usbipd and starts test H1.
# Start via h1_test.cmd. Before: load m1.bit via JTAG, THEN plug in the adapter's USB cable.
# The Windows part is logged to build\h1\h1_windows.log
$fw = Split-Path -Parent $PSScriptRoot
$log = Join-Path $fw "build\h1\h1_windows.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
Start-Transcript -Path $log -Force | Out-Null

function Stop-Here($msg) { Write-Host ""; Write-Host "ABORT: $msg"; Stop-Transcript | Out-Null; exit 1 }

$usbipd = (Get-Command usbipd -ErrorAction SilentlyContinue).Source
if (-not $usbipd) { $usbipd = "C:\Program Files\usbipd-win\usbipd.exe" }
if (-not (Test-Path $usbipd)) { Stop-Here "usbipd missing - run h1_prepare.cmd first." }

function Get-Line($busid) {
    # line of the usbipd list that starts with exactly this BUSID
    (& $usbipd list) | Where-Object { $_ -match ("^" + [regex]::Escape($busid) + "\s") } | Select-Object -First 1
}

Write-Host "USB devices (the USB-IDE adapter must appear here, otherwise check power/cable):"
& $usbipd list
Write-Host ""
$busid = Read-Host "Enter the BUSID of the USB-IDE adapter (e.g. 2-3)"
if ($busid -notmatch '^\d+-[\d\.]+$') { Stop-Here "Invalid BUSID '$busid'." }
$line = Get-Line $busid
if (-not $line) { Stop-Here "BUSID $busid is not in the list." }
Write-Host "Selected: $line"
if ($line -match 'DirtyJTAG') { Stop-Here "That is the JTAG adapter, not the USB-IDE adapter." }

# Share with --force (administrator rights): otherwise Windows keeps hold of mass storage devices.
# Case matters: "Not shared" is NOT shared.
if ($line -cnotmatch 'Attached' -and $line -cnotmatch 'forced') {
    # unbind a device that is already shared normally first, otherwise --force has no effect
    Write-Host "Sharing the device with --force (administrator prompt) ..."
    # elevated PowerShell instead of cmd /c (cmd mangles command lines that start with a quote)
    $cmd = "& '$usbipd' unbind --busid $busid; & '$usbipd' bind --force --busid $busid; Start-Sleep -Seconds 2"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-Command", $cmd -Verb RunAs -Wait
    Start-Sleep -Seconds 2
    $line = Get-Line $busid
    Write-Host "State: $line"
    if (-not $line -or $line -cnotmatch 'forced') {
        Write-Host "Please UNPLUG the adapter's USB cable briefly and PLUG IT BACK IN."
        Read-Host "Then press Enter"
        Start-Sleep -Seconds 3
        $line = Get-Line $busid
        Write-Host "State: $line"
    }
    if (-not $line -or $line -cmatch 'Not shared') { Stop-Here "Sharing failed: $line" }
}

Write-Host "Attaching the device to WSL ..."
# keep WSL running for the whole test (otherwise it shuts down after a short idle time)
$keep = Start-Process -FilePath "wsl.exe" -ArgumentList "-d","Ubuntu-22.04","--","sleep","1800" -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 3
if ($line -cnotmatch 'Attached') {
    & $usbipd attach --wsl --busid $busid
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Windows still holds the device. Please UNPLUG the adapter's USB cable briefly and PLUG IT BACK IN,"
        Read-Host "then press Enter"
        Start-Sleep -Seconds 3
        & $usbipd attach --wsl --busid $busid
        if ($LASTEXITCODE -ne 0) { Stop-Here "usbipd attach failed (code $LASTEXITCODE)." }
    }
}
Start-Sleep -Seconds 3
$line = Get-Line $busid
Write-Host "State: $line"
if ($line -cnotmatch 'Attached') { Stop-Here "Device is not attached to WSL." }

# run h1_test.sh from this script's folder (wsl --cd translates the Windows path)
& wsl -d Ubuntu-22.04 --cd $PSScriptRoot -- bash -c "sed -i 's/\r$//' h1_test.sh && bash h1_test.sh"

Write-Host ""
Write-Host "Detaching the device from WSL and returning it to Windows (administrator prompt) ..."
& $usbipd detach --busid $busid
Start-Process -FilePath $usbipd -ArgumentList "unbind","--busid",$busid -Verb RunAs -Wait
if ($keep -and -not $keep.HasExited) { Stop-Process -Id $keep.Id -Force -ErrorAction SilentlyContinue }
Write-Host "Done. Logs: build\h1\h1.log (Linux) and build\h1\h1_windows.log"
Stop-Transcript | Out-Null
