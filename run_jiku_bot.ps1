# 軸MTG Bot ラッパースクリプト - 環境変数を設定してから実行
$workDir = "C:\Users\makes\Desktop\AI\plaud-bot"
$logFile = "$workDir\jiku_bot.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log($msg) {
    $line = "[$timestamp] $msg"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

Write-Log "軸MTG Bot 開始"

# PLAUD_TOKEN を plaud_storage.json から読み込む
try {
    $storage = Get-Content "$workDir\plaud_storage.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $rawToken = $storage.pld_tokenstr
    $env:PLAUD_TOKEN = $rawToken -replace '^"(.+)"$', '$1'
    Write-Log "PLAUD_TOKEN: 読み込み成功"
} catch {
    Write-Log "ERROR: PLAUD_TOKEN の読み込みに失敗: $_"
    exit 1
}

# GOOGLE_CREDENTIALS_JSON
try {
    $env:GOOGLE_CREDENTIALS_JSON = Get-Content "C:\Users\makes\Desktop\AI\quixotic-module-496622-d2-a6e9852c355e.json" -Raw -Encoding UTF8
    Write-Log "GOOGLE_CREDENTIALS_JSON: 読み込み成功"
} catch {
    Write-Log "ERROR: GOOGLE_CREDENTIALS_JSON の読み込みに失敗: $_"
    exit 1
}

# LW_PRIVATE_KEY
try {
    $env:LW_PRIVATE_KEY = Get-Content "C:\Users\makes\Desktop\AI\private_20260518083822.key" -Raw -Encoding UTF8
    Write-Log "LW_PRIVATE_KEY: 読み込み成功"
} catch {
    Write-Log "ERROR: LW_PRIVATE_KEY の読み込みに失敗: $_"
    exit 1
}

# 固定値
$env:GOOGLE_DOCS_ID = "16g50b885ANqPrgP6u_iEmrtDu4xIKquhd3mi2JZlpk4"
$env:LW_JIKU_CH     = "335626540"

Write-Log "Python スクリプト実行開始"

# 実行してログに記録
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) {
    Write-Log "ERROR: python が見つかりません"
    exit 1
}

$output = & $pythonPath "$workDir\plaud_bot.py" 2>&1
$exitCode = $LASTEXITCODE

foreach ($line in $output) {
    Write-Log "  $line"
}

if ($exitCode -eq 0) {
    Write-Log "完了 (exit 0)"
} else {
    Write-Log "ERROR: exit code $exitCode"
}

exit $exitCode
