# MAIL MONSTER PRO — 운영 패키징 (PyInstaller + SHA256 + 선택 Inno)
# 실행: 프로젝트 루트에서  .\scripts\package_and_deploy.ps1
# 사전: .venv 에서 pip install -r requirements.txt 완료 권장

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    $Py = "python"
    Write-Warning ".venv\Scripts\python.exe 없음 — 시스템 python 사용"
}

Write-Host "== [1/4] PyInstaller 설치 확인 ==" -ForegroundColor Cyan
& $Py -m pip install --quiet pyinstaller

Write-Host "== [2/4] PyInstaller 빌드 ==" -ForegroundColor Cyan
& $Py -m PyInstaller MAIL_MONSTER_PRO.spec --noconfirm
$exe = Join-Path $Root "dist\MAIL_MONSTER_PRO.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "빌드 실패: dist\MAIL_MONSTER_PRO.exe 없음"
}
$len = (Get-Item -LiteralPath $exe).Length
Write-Host "OK: $exe ($len bytes)" -ForegroundColor Green

Write-Host "== [3/4] SHA256 (자동 업데이트용) ==" -ForegroundColor Cyan
$h = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$shaPath = Join-Path $Root "dist\MAIL_MONSTER_PRO.exe.sha256"
Set-Content -LiteralPath $shaPath -Value $h -Encoding ascii
Write-Host "OK: $shaPath" -ForegroundColor Green

Write-Host "== [4/4] Inno Setup (선택) ==" -ForegroundColor Cyan
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe" }
if (Test-Path $iscc) {
    & $iscc "installer\MAIL_MONSTER_PRO.iss"
    Write-Host "OK: dist\installer\" -ForegroundColor Green
} else {
    Write-Host "SKIP: Inno Setup 미설치 — Setup.exe 생략" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== 패키징 완료 ===" -ForegroundColor Green
Write-Host "  - $exe"
Write-Host "  - $shaPath"
Write-Host ""
Write-Host "GitHub 배포: git tag v2.6.2 && git push origin v2.6.2  (버전은 login.py CURRENT_VERSION 과 일치)" -ForegroundColor Gray
Write-Host "자세한 절차: RELEASE.md" -ForegroundColor Gray
