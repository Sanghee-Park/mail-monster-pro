# GitHub에 올리기 (mail-monster-pro)

## 중요: 비밀 정보

- **`config.json`에는 실제 SMTP 비밀번호가 들어 있습니다.** 저장소에는 **올리지 않습니다** (`.gitignore` 처리).
- 이미 실수로 푸시했다면 **메일 계정 비밀번호를 즉시 변경**하세요.

## 1. Git 설치

1. https://git-scm.com/download/win 에서 설치  
2. 설치 후 **Cursor/터미널을 완전히 닫았다가 다시 열기**  
3. 확인: `git --version`

## 2. SSH로 GitHub 연결 (이미 했다면 생략)

1. `ssh-keygen -t ed25519 -C "your_email@example.com"`  
2. 공개키(`~/.ssh/id_ed25519.pub`) 내용을 GitHub → **Settings → SSH keys**에 등록  
3. 확인: `ssh -T git@github.com`

## 3. 푸시 실행

**가장 쉬움:** 탐색기에서 `scripts\push_to_github.cmd` 더블클릭 (Git PATH가 잡히도록 `push_to_github.cmd` 안에서 보강함).

프로젝트 루트 (`이메일 발송` 폴더)에서 PowerShell:

```powershell
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

.\scripts\setup_git_and_push.ps1
```

수동으로 하려면:

```powershell
cd "프로젝트경로"
git init
git branch -M main
git remote add origin git@github.com:Sanghee-Park/mail-monster-pro.git
git add -A
git commit -m "chore: initial commit"
git push -u origin main
```

원격이 이미 있으면:

```powershell
git remote set-url origin git@github.com:Sanghee-Park/mail-monster-pro.git
git push -u origin main
```

**GitHub에서 README를 켜고 저장소를 만들었다면** 첫 푸시 전에:

```powershell
git pull origin main --allow-unrelated-histories
# 충돌 나면 해결 후
git push -u origin main
```

## 4. GitHub Actions (릴리스)

`v*` 태그를 푸시하면 `.github/workflows/release.yml`이 Windows에서 exe를 빌드해 Release에 올립니다.

```bash
git tag v2.5.2
git push origin v2.5.2
```
