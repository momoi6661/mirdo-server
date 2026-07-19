$ErrorActionPreference = "Stop"

$serverRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $serverRoot

uv run --with pyinstaller pyinstaller `
  --noconfirm `
  --onedir `
  --noconsole `
  --name MirdoServer `
  --copy-metadata genai-prices `
  --copy-metadata pydantic-ai-slim `
  --copy-metadata pydantic-ai `
  --add-data "data\knowledge;data\knowledge" `
  --add-data "data\tts;data\tts" `
  --add-data "data\dialogue;data\dialogue" `
  run_server.py

# PyInstaller onedir 将 --add-data 放到 _internal；运行时资源和可写 runtime
# 需要放在 MirdoServer.exe 同级，因此把静态数据复制到根目录。
$bundleRoot = Join-Path $serverRoot "dist\MirdoServer"
foreach ($name in @("knowledge", "tts", "dialogue")) {
  $target = Join-Path $bundleRoot "data\$name"
  if (Test-Path $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  Copy-Item -LiteralPath (Join-Path $serverRoot "data\$name") -Destination $target -Recurse
}

Write-Host "Built: $bundleRoot\MirdoServer.exe"
