# mail-monster-pro 최초 GitHub 푸시 (SSH)
# 실행: 프로젝트 루트에서  .\scripts\setup_git_and_push.ps1
# (Git 설치 직후 Cursor를 안 껐다면 PATH가 비어 있을 수 있음 → 아래에서 Machine+User PATH를 다시 불러옴)

$ErrorActionPreference = "Stop"
# SSH 미설정 PC에서는 HTTPS가 더 잘 동작합니다(Windows 자격 증명 관리자).
$RemoteUrl = "https://github.com/Sanghee-Park/mail-monster-pro.git"

# 터미널이 예전 PATH로 떠 있는 경우 대비: 레지스트리 기준 PATH로 갱신
$machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$extraGit = @(
    "${env:ProgramW6432}\Git\cmd",
    "${env:ProgramW6432}\Git\bin",
    "${env:ProgramFiles}\Git\cmd",
    "${env:ProgramFiles}\Git\bin",
    "${env:ProgramFiles(x86)}\Git\cmd",
    "${env:LOCALAPPDATA}\Programs\Git\cmd",
    "${env:LOCALAPPDATA}\Programs\Git\bin"
) -join ";"
$env:Path = ($machinePath, $userPath, $extraGit) -join ";"

function Get-GitExecutable {
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $where = (& cmd.exe /c "where git 2>nul" | Select-Object -First 1)
    if ($where -and (Test-Path -LiteralPath $where.Trim())) { return $where.Trim() }

    $candidates = @(
        "${env:ProgramW6432}\Git\cmd\git.exe",
        "${env:ProgramFiles}\Git\cmd\git.exe",
        "${env:ProgramFiles}\Git\bin\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "${env:LOCALAPPDATA}\Programs\Git\cmd\git.exe",
        "${env:LOCALAPPDATA}\Programs\Git\bin\git.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }

    foreach ($hive in @("HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")) {
        Get-ItemProperty $hive -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -match 'Git for Windows' -and $_.InstallLocation } |
            ForEach-Object {
                foreach ($sub in @("cmd\git.exe", "bin\git.exe")) {
                    $g = Join-Path $_.InstallLocation $sub
                    if (Test-Path -LiteralPath $g) { return $g }
                }
            }
    }
    return $null
}

$gitExe = Get-GitExecutable
if (-not $gitExe) {
    Write-Host ""
    Write-Host "Git을 찾지 못했습니다. 다음을 확인하세요:" -ForegroundColor Yellow
    Write-Host "  1) https://git-scm.com/download/win 에서 설치 (기본 경로 권장)"
    Write-Host "  2) 설치 시 'Git from the command line and also from 3rd-party software' 선택"
    Write-Host "  3) Cursor 전체 종료 후 다시 열기"
    Write-Host ""
    Write-Host "수동 확인: 아래를 별도 CMD에서 실행했을 때 경로가 나와야 합니다."
    Write-Host "  where git"
    Write-Error "[Git not found]"
}

Write-Host "Using Git: $gitExe" -ForegroundColor Cyan

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

if (-not (Test-Path ".git")) {
    & $gitExe init
    & $gitExe branch -M main
}

if (-not (& $gitExe config --get user.email)) {
    if (-not (& $gitExe config --global --get user.email)) {
        & $gitExe config user.email "Sanghee-Park@users.noreply.github.com"
        & $gitExe config user.name "Sanghee-Park"
    }
}

& $gitExe remote remove origin 2>$null
& $gitExe remote add origin $RemoteUrl

& $gitExe add -A
$status = & $gitExe status --porcelain
if ($status) {
    & $gitExe commit -m "chore: initial project setup for mail-monster-pro"
}

& $gitExe push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error "git push 실패. SSH 키가 GitHub에 등록됐는지 확인: ssh -T git@github.com"
}
Write-Host "완료: $RemoteUrl (main)" -ForegroundColor Green
