param(
    [string]$Workspace = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'DailyArxiv'),
    [switch]$RunNow,
    [switch]$Status
)
$ErrorActionPreference = 'Stop'
$codeDirectory = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python = Join-Path $codeDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'DailyArxiv Python environment is missing. Run the installer first.' }
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
$env:PYTHONUTF8 = '1'
$env:GCM_INTERACTIVE = 'never'
$env:GIT_TERMINAL_PROMPT = '0'
$env:OPENAI_BASE_URL = 'https://api.deepseek.com'
$env:MODEL_NAME = 'deepseek-v4-pro'
$env:LANGUAGE = 'Chinese'
$env:CATEGORIES = 'cs.CV,cs.AI,cs.LG'
$env:RELEVANCE_MODE = 'semantic'
$configPath = Join-Path $Workspace 'config.json'
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    foreach ($name in @('OPENAI_BASE_URL', 'MODEL_NAME', 'LANGUAGE', 'CATEGORIES', 'RELEVANCE_MODE')) {
        if ($config.$name) { [Environment]::SetEnvironmentVariable($name, [string]$config.$name, 'Process') }
    }
}
$arguments = @('-m', 'local_runner.scheduler', '--workspace', $Workspace)
if ($RunNow) { $arguments += '--run-now' }
if ($Status) { $arguments += '--status' }
$exitCode = 1
try {
    $secretPath = Join-Path $Workspace 'api-key.dpapi'
    if (-not $Status -and (Test-Path -LiteralPath $secretPath)) {
        $secure = ConvertTo-SecureString (Get-Content -Raw -LiteralPath $secretPath)
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer); $secure.Dispose() }
    }
    Push-Location $codeDirectory
    try {
        $logDirectory = Join-Path $Workspace 'logs'
        New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
        $logPath = Join-Path $logDirectory ((Get-Date -Format 'yyyy-MM-dd') + '.log')
        # PowerShell 5 treats a native stderr line as NativeCommandError even
        # when Python exits successfully. Use the exit code as the result.
        $ErrorActionPreference = 'Continue'
        try {
            if ($Status) { & $python @arguments }
            else { & $python @arguments >> $logPath 2>&1 }
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = 'Stop'
        }
    } finally { Pop-Location }
} finally { Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue }
exit $exitCode
