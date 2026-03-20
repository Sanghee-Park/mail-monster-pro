# MAIL MONSTER PRO V3.0 – 빌드 및 패키징

## 1. 환경 준비

- Python 3.10 ~ 3.12 권장 (3.14는 pywebview 빌드 이슈 가능)
- 가상환경 생성 후 의존성 설치:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

- **pywebview** (WYSIWYG 에디터): Windows에서 `pip install pywebview` 실패 시, 미설치해도 앱은 실행되며 에디터만 비활성됩니다. 필요 시 [pywebview 릴리스](https://github.com/r0x0r/pywebview/releases)에서 wheel을 설치하거나, WebView2 런타임이 설치된 환경에서 재시도하세요.

## 2. 실행 검증

```bash
.venv\Scripts\python main.py
```

- 로그인 창 → 로그인 성공 후 메인 화면(MAIL MONSTER PRO)이 뜨면 정상입니다.

## 3. 단일 실행 파일(EXE) 패키징

1. PyInstaller 설치:

```bash
pip install pyinstaller
```

2. 스펙으로 빌드:

```bash
pyinstaller MAIL_MONSTER_PRO.spec
```

3. 결과물: `dist\MAIL_MONSTER_PRO.exe`

4. **배포 시 함께 둘 파일**
   - `dist\MAIL_MONSTER_PRO.exe`
   - `wysiwyg_editor.html` (exe와 같은 폴더에 두면 WYSIWYG 에디터 사용 가능)
   - (선택) `pro.ico` – 아이콘 미포함 시 제외 가능
   - (선택) `credentials.json` – 구글 시트 로그인 사용 시
   - 실행 후 생성되는 파일: `config.json`, `templates.json`, `recipients.json`, `login_settings.json`, `sent_history.db` 등은 exe와 같은 폴더에 자동 생성됩니다.

## 4. 배포용 폴더 구성 (v2.5)

빌드 후 `dist` 폴더에 다음을 두고 배포하면 됩니다.

| 파일 | 필수 여부 | 비고 |
|------|-----------|------|
| `MAIL_MONSTER_PRO.exe` | ✅ 필수 | 단일 실행 파일 |
| `wysiwyg_editor.html` | 권장 | WYSIWYG 에디터 사용 시 (없으면 에디터만 비활성) |
| `credentials.json` | 구글 사용 시 | 로그인/블랙리스트/업데이트용 구글 시트 연동 |
| `pro.ico` | 선택 | 아이콘 없으면 기본 아이콘 |

실행 후 exe와 같은 폴더에 자동 생성: `config.json`, `templates.json`, `recipients.json`, `login_settings.json`, `sent_history.db` 등.

## 5. 요약

| 항목 | 내용 |
|------|------|
| 진입점 | `main.py` (LoginApp → ModernMailSender) |
| 버전 표시 | v2.5.1 (로그인/메인 창 타이틀) |
| Plan 진행 | Phase 1~3 (v2.5.1 안정화) 완료 (상세는 `plan.md` 참고) |

**v2.5.1 패키징**: 프로젝트 `.venv`에서 `pyinstaller MAIL_MONSTER_PRO.spec --noconfirm` 실행 시 `dist\MAIL_MONSTER_PRO.exe`가 생성됩니다. (conda 전역 환경 사용 시 pywebview/PyQt 등으로 빌드가 매우 오래 걸릴 수 있음.)

## 6. Phase 5 — Inno Setup 설치 파일 & GitHub 릴리스

### 6-1. Inno Setup으로 `Setup.exe` (Task 5-1)

1. [Inno Setup 6](https://jrsoftware.org/isdl.php) 설치.
2. PyInstaller로 `dist\MAIL_MONSTER_PRO.exe` 생성 (위 3절).
3. PowerShell에서 프로젝트 루트로 이동 후:

```powershell
.\scripts\build_installer.ps1
```

- 성공 시: `dist\installer\MAIL_MONSTER_PRO_Setup_2.5.2.exe` (버전은 `installer\MAIL_MONSTER_PRO.iss`의 `#define MyAppVersion`과 맞출 것)
- `pro.ico`가 없으면 `installer\MAIL_MONSTER_PRO.iss`의 `SetupIconFile` 줄을 제거하거나 주석 처리.

### 6-2. GitHub Actions 자동 릴리스 (Task 5-2)

- 태그를 푸시하면 Windows에서 빌드 후 **Release**에 `MAIL_MONSTER_PRO.exe`가 첨부됩니다.

```bash
git tag v2.5.3
git push origin v2.5.3
```

- 저장소가 GitHub에 있어야 하며, 워크플로: `.github/workflows/release.yml`

### 6-3. 업데이트 URL & GitHub 연동 (Task 5-3)

- **수동**: 릴리스에 올라간 exe의 **브라우저 다운로드 URL**을 구글 시트 `설정` 탭 **B1**에 붙여넣기.
- **CLI**: 최신 릴리스 exe URL만 출력 (시트에 복사용)

```bash
python scripts/github_latest_release_url.py your-org/your-repo
```

- **앱 내 폴백**: 환경변수 `MAILMONSTER_GITHUB_REPO=owner/repo` 를 설정하고, 시트 **B1이 비어 있으면** GitHub API로 최신 릴리스의 `.exe` 다운로드 URL을 사용합니다. (시트 A1 버전과 비교하는 기존 로직은 동일)
