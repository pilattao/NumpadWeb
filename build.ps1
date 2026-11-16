# Build WebNumpad.exe.  Run: powershell -ExecutionPolicy Bypass -File build.ps1
# (ASCII only: Windows PowerShell 5 reads scripts without BOM as ANSI.)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv\Scripts\python.exe)) {
    py -3 -m venv .venv
}
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check pyinstaller segno
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

.venv\Scripts\python.exe -c "import numpad_core as c; open('icon.ico','wb').write(c.make_ico())"
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --clean --log-level WARN `
    --name WebNumpad --icon "$PSScriptRoot\icon.ico" `
    --add-data "$PSScriptRoot\index.html;." `
    --exclude-module PIL --exclude-module zxingcpp `
    --workpath build --distpath dist --specpath build `
    numpad_app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item dist\WebNumpad.exe .\WebNumpad.exe -Force
Write-Host "Done: $PSScriptRoot\WebNumpad.exe"
