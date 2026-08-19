# Обёртка над deploy/deploy.sh для PowerShell: находит bash из Git for Windows
# и передаёт ему все аргументы. Переменные окружения (DEPLOY_HOST, DEPLOY_SSH_KEY
# и прочие) наследуются как есть.
#
#   $env:DEPLOY_HOST = "1.2.3.4"
#   $env:DEPLOY_SSH_KEY = "$env:USERPROFILE\.ssh\timeweb_stage"
#   .\deploy\deploy.ps1
#   .\deploy\deploy.ps1 --logs 100
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ScriptArgs)

$ErrorActionPreference = 'Stop'

$bash = $null
foreach ($candidate in @(
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
)) {
    if (Test-Path $candidate) { $bash = $candidate; break }
}
if (-not $bash) {
    $cmd = Get-Command bash.exe -ErrorAction SilentlyContinue
    if ($cmd) { $bash = $cmd.Source }
}
if (-not $bash) {
    throw "Не найден bash.exe. Установи Git for Windows (https://git-scm.com/download/win) или запусти deploy/deploy.sh из Git Bash."
}

$sh = Join-Path $PSScriptRoot 'deploy.sh'
& $bash $sh @ScriptArgs
exit $LASTEXITCODE
