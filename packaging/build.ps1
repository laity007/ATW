# Build the Windows one-file executable.
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $Root
try {
    & .\.venv\Scripts\pyinstaller.exe packaging\atw.spec --noconfirm --distpath dist\windows --workpath build\pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    & .\dist\windows\atw.exe --version
    Write-Host "built dist\windows\atw.exe"
} finally {
    Pop-Location
}
