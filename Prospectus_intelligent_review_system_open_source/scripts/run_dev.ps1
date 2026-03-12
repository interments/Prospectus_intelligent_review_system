$ErrorActionPreference = "Stop"

$root = "D:\OpenClaw\openclaw-main\workspace\Prospectus_intelligent_review_system"
Set-Location $root

if (!(Test-Path ".\.venv-win")) {
  py -3 -m venv .venv-win
}

.\.venv-win\Scripts\python -m pip install -r .\backend\requirements.txt

Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd $root\backend; ..\.venv-win\Scripts\python -m app.server"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd $root; py -3 -m http.server 8080"

Write-Host "Backend: http://localhost:9000"
Write-Host "Frontend: http://localhost:8080/frontend/"
