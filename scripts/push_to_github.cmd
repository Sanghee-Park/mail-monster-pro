@echo off
chcp 65001 >nul
REM 64bit 기본 설치 경로 + winget 로컬 경로
set "PATH=%PATH%;%ProgramW6432%\Git\cmd;%ProgramW6432%\Git\bin;%ProgramFiles%\Git\cmd;%ProgramFiles%\Git\bin;%ProgramFiles(x86)%\Git\cmd;%LocalAppData%\Programs\Git\cmd;%LocalAppData%\Programs\Git\bin"
cd /d "%~dp0.."
echo Running setup_git_and_push.ps1 ...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_git_and_push.ps1"
pause
