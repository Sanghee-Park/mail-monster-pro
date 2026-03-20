# Task 5-1: PyInstaller 빌드 후 Inno Setup으로 Setup.exe 생성
# 사용: 프로젝트 루트에서 .\scripts\build_installer.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[1/2] PyInstaller (MAIL_MONSTER_PRO.spec)..." -ForegroundColor Cyan
python -m pip install --quiet pyinstaller
pyinstaller MAIL_MONSTER_PRO.spec --noconfirm
if (-not (Test-Path "dist\MAIL_MONSTER_PRO.exe")) {
    throw "dist\MAIL_MONSTER_PRO.exe 가 생성되지 않았습니다."
}

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    $iscc = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $iscc)) {
    Write-Warning "Inno Setup 6(ISCC.exe)을 찾을 수 없습니다. https://jrsoftware.org/isdl.php 에서 설치 후 다시 실행하세요."
    Write-Host "EXE만 생성됨: $Root\dist\MAIL_MONSTER_PRO.exe"
    exit 0
}

Write-Host "[2/2] Inno Setup 컴파일..." -ForegroundColor Cyan
& $iscc "installer\MAIL_MONSTER_PRO.iss"
Write-Host "완료: dist\installer\MAIL_MONSTER_PRO_Setup_*.exe" -ForegroundColor Green
