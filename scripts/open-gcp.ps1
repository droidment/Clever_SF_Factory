$ErrorActionPreference = 'Stop'
# This authenticates the browser's traffic; the application and database run in GCP.
$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
& $python (Join-Path $PSScriptRoot 'gcp_proxy.py')
if ($LASTEXITCODE -ne 0) { throw 'Cloud Run proxy stopped with an error.' }
