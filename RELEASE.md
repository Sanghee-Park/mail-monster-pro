# MAIL MONSTER PRO — 운영 배포 체크리스트 (v2.6.6 기준)

## 1. 버전 한 곳에서 맞추기

배포 전 아래가 **같은 버전**인지 확인합니다.

| 위치 | 형식 예 |
|------|---------|
| `login.py` → `CURRENT_VERSION` | `v2.6.6` |
| `installer/MAIL_MONSTER_PRO.iss` → `#define MyAppVersion` | `2.6.6` (v 없음) |
| 구글 시트 `설정` **A1** | `v2.6.6` (사용자에게 보이는 최신 버전) |
| Git 태그 | `v2.6.6` |

## 2. 로컬 패키징 (Windows)

프로젝트 루트에서 PowerShell:

```powershell
.\scripts\package_and_deploy.ps1
```

생성물:

- `dist\MAIL_MONSTER_PRO.exe` — 배포용 단일 실행 파일  
- `dist\MAIL_MONSTER_PRO.exe.sha256` — 자동 업데이트 무결성용 (GitHub Release에 함께 올림)  
- (Inno Setup 설치 시) `dist\installer\MAIL_MONSTER_PRO_Setup_2.6.6.exe`

## 3. GitHub Release (자동 업데이트 연동)

1. 변경사항 커밋 후 태그 생성·푸시:

```bash
git add -A
git commit -m "release: v2.6.6"
git tag v2.6.6
git push origin main
git push origin v2.6.6
```

2. Actions **Build and Release** 가 `MAIL_MONSTER_PRO.exe` + `.sha256` 을 Release에 올립니다.

3. 시트 **B1** 은 비우거나 `GITHUB` → 앱이 GitHub 최신 릴리스 URL을 사용합니다.

### Release가 GitHub에 안 보일 때

- **정상 지연**: 태그를 올린 직후 **Releases** 탭에는 아무 것도 없을 수 있습니다. 워크플로가 **PyInstaller**를 돌리는 동안(대략 **10~20분**)은 Release가 아직 **생성되지 않습니다**. 마지막 단계 **Upload release assets**가 성공해야 `v2.6.6` Release와 exe가 보입니다.
- **태그만 먼저 확인**: 저장소 **Code → 태그**에 `v2.6.6`가 있으면 푸시는 된 것입니다. Release는 Actions 성공 후에 나타납니다.
- **Actions 실패**: **Actions** → **Build and Release (Windows)** → 실패한 실행 → 로그에서 `PyInstaller` 또는 `Install dependencies` 오류 확인. 워크플로에 **Run workflow**가 있으면 동일 태그(예: `v2.6.6`)를 넣고 수동 재실행할 수 있습니다.

## 4. 사용자에게 같이 줄 것

- `MAIL_MONSTER_PRO.exe` 또는 Setup.exe  
- (구글 시트 로그인용) 서비스 계정 **`credentials.json`** 은 사용자별로 안내 (저장소에 넣지 않음)  
- `config.example.json` → 복사 후 `config.json` 작성 안내  

## 5. 참고 문서

- [BUILD.md](BUILD.md) — 환경·빌드 상세  
- [UPDATE_VIA_GITHUB.md](UPDATE_VIA_GITHUB.md) — GitHub 전용 업데이트  
- [DEPLOY_GITHUB.md](DEPLOY_GITHUB.md) — Git 원격·푸시  
