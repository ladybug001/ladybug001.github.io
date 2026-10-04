$ErrorActionPreference = 'Stop'
$previewPython = Join-Path $PSScriptRoot '.local\publisher-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $previewPython -PathType Leaf)) {
    throw 'Pinned local Python environment is missing. See docs/hugo-stack.md.'
}
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $previewPython -B pipeline/site_preview.py --serve --port 8085
    if ($LASTEXITCODE -ne 0) { throw 'Stack preview failed; inspect the reported error.' }
} finally {
    Pop-Location
}
