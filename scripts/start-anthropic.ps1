param([ValidateRange(1024, 65535)][int]$Port = 8001)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project Python environment first; see README.md.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Choose another port with -Port."
}
$names = @('FACTORY_ANTHROPIC_API_KEY', 'FACTORY_MODE', 'FACTORY_AUTH_MODE', 'FACTORY_DATABASE_URL', 'FACTORY_DISPATCH_ENABLED')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
Write-Host 'Anthropic draft workspace: real paid model calls begin when you submit a request.'
Write-Host 'The key is masked and held only in this process and its child, not saved to disk.'
$secret = Read-Host 'Anthropic API key' -AsSecureString
$pointer = [IntPtr]::Zero
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    $env:FACTORY_ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($env:FACTORY_ANTHROPIC_API_KEY)) { throw 'An API key is required.' }
    $env:FACTORY_MODE = 'anthropic'
    $env:FACTORY_AUTH_MODE = 'demo'
    $env:FACTORY_DATABASE_URL = 'sqlite:///./data/anthropic.db'
    $env:FACTORY_DISPATCH_ENABLED = 'true'
    Push-Location -LiteralPath $workspace
    try {
        Write-Host "Open http://127.0.0.1:$Port and submit a new request. Ctrl+C stops the server."
        & $python -m uvicorn factory.api:app --host 127.0.0.1 --port $Port --no-access-log
        if ($LASTEXITCODE -ne 0) { throw 'Server exited with an error; review the message above.' }
    } finally { Pop-Location }
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    $secret.Dispose()
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
