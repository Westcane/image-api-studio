$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectPath
try {
    python -m PyInstaller --noconfirm --onefile --windowed --noupx --name '绘图小屋' --distpath $projectPath --workpath (Join-Path $PSScriptRoot 'build') --specpath $PSScriptRoot --paths $projectPath --hidden-import app.gemini_backend --hidden-import app.packaging_check --exclude-module pandas --exclude-module numpy --exclude-module matplotlib --exclude-module scipy --exclude-module torch --exclude-module tensorflow --exclude-module IPython --exclude-module pytest (Join-Path $PSScriptRoot 'entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
} finally {
    Pop-Location
}
