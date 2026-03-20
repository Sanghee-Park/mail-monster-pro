# GitHub만으로 업데이트 배포하기 (구글 드라이브 불필요)

## 동작 요약

1. **구글 시트 `설정`**  
   - **A1**: 사용자에게 안내할 **최신 버전** (예: `v2.5.3`) — 앱의 `login.py` 안 `CURRENT_VERSION`과 비교  
   - **B1**: 비워 두거나 **`GITHUB`** 만 입력 → 앱이 GitHub **최신 Release**에서 `MAIL_MONSTER_PRO.exe` 다운로드 URL을 자동 조회  
   - B1에 **직접 URL**을 넣으면 그 주소가 우선 (수동 덮어쓰기)

2. **릴리스 올리기**  
   - 태그 푸시 → `.github/workflows/release.yml` 이 exe 빌드 후 Release에 첨부  
   - 예: `git tag v2.5.3 && git push origin v2.5.3`

3. **시트 A1** 을 방금 올린 버전과 맞추기 (예: `v2.5.3`)

## 어떤 저장소를 볼지

우선순위:

1. 환경 변수 `MAILMONSTER_GITHUB_REPO=owner/repo` (배치/단축아이콘에서 지정 가능)  
2. 실행 폴더의 **`github_release_repo.txt`** 첫 번째 유효 줄 (`owner/repo`)  
3. 코드 기본값: **`Sanghee-Park/mail-monster-pro`** (`login.py`의 `GITHUB_RELEASE_REPO_DEFAULT`)

GitHub API만 쓰고 싶지 않을 때: 환경 변수 **`MAILMONSTER_DISABLE_GITHUB_RELEASE=1`**

## 시트 예시 (드라이브 링크 없음)

| A1 (버전) | B1 (URL)   |
|-----------|------------|
| v2.5.3    | *(비움)* 또는 `GITHUB` |

## 자동 업데이트(무료로 할 수 있는 것)

릴리스 워크플로가 **`MAIL_MONSTER_PRO.exe.sha256`** 을 같이 올립니다. 앱은 GitHub에서 exe를 받은 뒤 **SHA256이 일치할 때만** 교체를 진행합니다(변조·깨진 다운로드 방지).

- 다운로드 후 Windows **인터넷 보안 표시(Zone)** 를 풀기 위해 **`Unblock-File`** 을 실행합니다. **코드 서명 없이** SmartScreen 경고를 없앨 수는 없지만, 일부 PC에서 “차단된 파일” 메시지를 줄이는 데 도움이 될 수 있습니다.
- 자동 다운로드가 실패하면 **GitHub 릴리스 페이지**를 브라우저로 열고, 안내에 **「추가 정보 → 실행」** 을 넣어 두었습니다.
- **완전 무료**로는 Microsoft SmartScreen을 없앨 수 없습니다. 회사 배포라면 나중에 코드 서명을 검토하세요.

## 주의

- Release에 **`MAIL_MONSTER_PRO.exe`** 와 **`MAIL_MONSTER_PRO.exe.sha256`** 이 있어야 자동 업데이트 검증이 가장 안전합니다 (sha256 파일이 없으면 검증만 생략되고 다운로드는 진행).  
- GitHub API 비로그인 호출은 **시간당 횟수 제한**이 있으나, 로그인 시 업데이트 확인 1회 수준에서는 보통 문제 없습니다.  
- 새 버전을 낼 때마다 **`login.py`의 `CURRENT_VERSION`** 과 빌드된 앱 버전을 **A1과 맞추는 것**을 권장합니다.
