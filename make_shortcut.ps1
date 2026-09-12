# make_shortcut.ps1 - puts a "Jarvis" shortcut with the orb icon on your Desktop (and in this folder).
#
# Right-click this file and choose "Run with PowerShell". If Windows blocks it, run:
#     powershell -ExecutionPolicy Bypass -File .\make_shortcut.ps1
#
# The shortcut runs launch.pyw with the venv's pythonw.exe: no console window, and it always starts
# whatever code is in this folder, so it never needs rebuilding after a change.

$ErrorActionPreference = "Stop"
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$pythonw = Join-Path $here "venv\Scripts\pythonw.exe"
$launcher = Join-Path $here "launch.pyw"
$icon = Join-Path $here "ui\jarvis.ico"

if (-not (Test-Path $pythonw)) {
    throw "No virtual environment at $pythonw. Make one first: python -m venv venv"
}
foreach ($needed in @($launcher, $icon)) {
    if (-not (Test-Path $needed)) { throw "Missing $needed - is this the JARVIS folder?" }
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath("Desktop"), $here)) {
    $path = Join-Path $folder "Jarvis.lnk"
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + $launcher + '"'
    $link.WorkingDirectory = $here
    $link.IconLocation = "$icon,0"
    $link.Description = "J.A.R.V.I.S dashboard"
    $link.Save()
    Write-Host "Created $path"
}

Write-Host ""
Write-Host "Double-click Jarvis on your Desktop to start it."
Write-Host "To pin it: right-click the shortcut -> Pin to taskbar (or Pin to Start)."
Write-Host "If nothing happens, open jarvis.log in this folder - that is where errors go now."
