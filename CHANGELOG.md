# MAIL MONSTER PRO 변경 이력

## [v2.6.1] - 2026-03-20

### Phase 1 Task 1-1 — 글로벌 중복 발송(템플릿 중심)
- **타 담당자 차단 폐지**: `sent_log`에 다른 `sender`가 있어도, **동일 수신 이메일 + 동일 템플릿명**이 아니면 발송 허용.
- **`check_duplicate_send_status`**: 담당자와 무관하게 `email` + `template_name`(대소문자·앞뒤 공백 무시) 일치 행이 하나라도 있을 때만 스킵. 반환값 `(스킵, 사유, 이전_발송자_표시명)` — 스킵 시 로그에 기록된 담당자 표시.
- **스킵 로그 형식**: `🚫 스킵: {업체명} (이미 동일 템플릿 발송됨 - 담당자: {sender}) …`
- **버전 정렬**: `login.py` / `main_ui.py` / Inno `MyAppVersion` → **v2.6.1**

### Phase 2 Task 2-1 · 2-2 — 발송자 프로필 (계정별)
- **[⚙ 계정 설정]** 탭에 **발송자 정보** 그룹: 이름(`user_name`), 직책(`user_rank`), 전화번호(`user_phone`), 이메일(`user_email`).
- **`config.json`**: 계정 키(예: `외부메일_1`)마다 **`sender_profile`** 객체로 저장. **발송자 정보만 저장** 버튼으로 SMTP 없이도 저장 가능.
- **SMTP 연동 저장** 시 기존 `sender_profile`은 유지(입력 칸이 비어 있으면 이전 값 유지).
- **`get_sender_profile(task_key)`**: Phase 3 변수 치환에서 사용할 조회용 API.
- **`config.example.json`**: `sender_profile` 예시 추가.

### Phase 3 Task 3-1 · 3-2 — 내 정보 변수 치환
- **`replace_user_variables(text, task_key)` 추가**: `{{내이름}}`, `{{내직책}}`, `{{내전화번호}}`, `{{내이메일}}` 태그를 계정별 `sender_profile` 값으로 치환.
- **발송 직전 치환 적용**: `real_engine`에서 엑셀 변수 치환 후 내 정보 변수 치환을 추가해 제목·본문 모두 반영.
- **에디터 가이드 라벨 추가**: 메시지 발송 탭에 사용 가능한 내 정보 태그 목록 표시.
- **추가 UX 보강**: 메시지 탭에 태그 **빠른 삽입 버튼**(`{{내이름}}` 등) 추가.
- **안전장치 보강**: 태그를 사용했는데 프로필 값이 비어 있으면 발송 시작 전에 누락 항목 확인 팝업 제공.
- **보내는 사람 이름 자동 보정**: 입력칸이 비어 있으면 계정 `sender_profile.user_name`(없으면 로그인 사용자명)으로 대체.
- **미리보기 확장**: 메시지 탭에 **`🔍 미리보기`** 버튼 추가, 샘플 수신처 기준 최종 치환(엑셀+내정보) 결과를 제목/본문으로 확인 가능.
- **치환 일관성 통합**: `_render_message_with_variables()` 공통 함수로 실제 발송(`real_engine`)과 테스트 발송(`_start_test_send`)에 동일한 치환 경로 적용.
- **미리보기 고도화**: 수신처 탭에서 선택한 행을 샘플 데이터로 우선 사용하고, 없으면 첫 행/기본 샘플로 폴백.
- **미치환 태그 경고**: 미리보기에서 `{{...}}` 패턴이 남아 있으면 경고 라벨로 즉시 안내.
- **브라우저 렌더 보기**: 미리보기 팝업에서 `🌐 브라우저 렌더 보기` 버튼으로 실제 HTML 렌더 결과를 외부 브라우저에서 확인 가능.

## [v2.6.0] - 2026-03-18

### 발송·중복 규칙
- **수신처(이메일) 기준**: SMTP 계정(`task_key`)당 1템플릿 제한 **제거**. 같은 수신처에 **다른 템플릿**은 발송 가능, **동일 템플릿**만 `sent_log`로 스킵.
- **1수신처 1담당자**: `sent_log.sender`가 비어 있지 않고 현재 로그인 담당자와 다르면 타 담당자 영역으로 차단(기존 `check_duplicate_send_status` 규칙 유지).

### 운영 패키징·배포
- **릴리스 버전 정렬**: `login.py` / `main_ui.py` / Inno `MyAppVersion` → **v2.6.0**
- **`scripts/package_and_deploy.ps1`**: PyInstaller + `MAIL_MONSTER_PRO.exe.sha256` + 선택 Inno Setup
- **`RELEASE.md`**: 시트 A1·태그·GitHub Actions 순서 포함 운영 체크리스트
- GitHub Actions Release에 **exe + sha256** 동시 업로드(자동 업데이트 무결성)

## [v2.5.2] - 2025-03-19

### 버그 수정
- **Task 1-1 (버전 문자열 비교 오류 수정)**
  - 로컬 버전과 구글 시트 버전 비교 시 공백·대소문자 차이로 '다른 버전'으로 잘못 인식되던 문제 해결
  - `_normalize_version_for_compare()`에 `.strip().lower()` 강화: NBSP, 탭, 개행 등 보이지 않는 공백 제거
  - 시트에서 읽은 값에 `str().strip()` 적용하여 숫자/타입 차이로 인한 오류 방지
  - 버전이 동일하면 업데이트 팝업 없이 즉시 메인 화면 진입 보장
- **버전 비교 추가 보강 (v2.5.2 후속)**
  - zero-width·BOM·NFKC 정규화로 시트 복사 값과 앱 문자열 비교 안정화
  - 숫자 세그먼트 튜플 비교 (`2.5.2` ↔ `v2.5.2` 등)로 동일 버전 판별
  - 로그인 후 업데이트 검사: **A1이 앱과 같으면 B1(URL) 유무와 관계없이 즉시 메인 실행** (동일 버전인데도 권고 팝업이 뜨던 순서 수정)
- **Task 1-2 (일부)**: 자동 다운로드 실패 시 `webbrowser.open()`으로 시트 링크 열기 + 안내 메시지

### Phase 2
- **Task 2-1 (수신처 삭제 UI–Data 동기화)**  
  - **[선택 행 삭제]** 시 Treeview에서 제거한 행과 동일한 인덱스의 `recipients.json` `rows` 항목을 `pop`하여 저장  
  - 트리 행 수와 `rows` 길이가 맞지 않을 때는 트리 표시 순서로 `rows`를 재구성하는 보정 로직 추가
- **Task 2-2 (템플릿 기반 중복 발송 로직)**  
  - `_dedup_template_key()`: 저장된 템플릿명 우선, 없으면 제목으로 동일 키 생성·`strip()` 통일  
  - `start()`·`real_engine`에서 동일 키로 `current_template_name` / `actual_template` / `record_success_to_db` 연계  
  - `sent_log` INSERT·중복 `SELECT`에 `TRIM` 반영해 이전 행 공백과도 매칭, 1계정 1템플릿 검사도 `TRIM` 기준으로 정합

### Phase 3 (글로벌 발송 이력)
- **Task 3-1**: `sent_log`에 `sender` 컬럼 추가, 시트 `발송내역` 없으면 시트·헤더 자동 생성, 기동 시 시트→로컬 DB 병합 동기화  
- **Task 3-2**: `check_duplicate_send_status()` — 타 담당자 이력이 있으면 차단, 본인·레거시만 있을 때 동일 템플릿이면 차단  
- **Task 3-3**: 발송 성공 시 `append_row`로 `[시간, 담당자, 업체명, 이메일, 템플릿명]` 기록  
- `record_success_to_db`에 현재 로그인 담당자(`user_name`)를 `sender`로 저장

### Phase 4 (정리·Task 4-4 보강)
- **Task 4-1 ~ 4-3**: Phase 1·2·3 구현과 동일 요구사항으로 검증 완료(버전 비교·수신처 삭제 동기·발송내역 시트·2중 필터)
- **Task 4-4**: `_effective_template_for_log()` — 템플릿명이 비면 제목으로 대체해 DB·구글 시트 `append_row`에 빈 템플릿명 방지  
- 중복 스킵·1계정 1템플릿 거부 로그에 **사유·템플릿 키** 표시로 가독성 개선

### Phase 5 (배포·GitHub)
- **Task 5-1**: `installer/MAIL_MONSTER_PRO.iss` (Inno Setup 6), `scripts/build_installer.ps1` — `dist\installer\MAIL_MONSTER_PRO_Setup_*.exe` 생성  
- **Task 5-2**: `.github/workflows/release.yml` — `v*` 태그 푸시 시 PyInstaller 빌드 후 Release에 `MAIL_MONSTER_PRO.exe` 업로드  
- **Task 5-3**: `scripts/github_latest_release_url.py` — 최신 릴리스 exe URL 출력; `login.py`에서 `MAILMONSTER_GITHUB_REPO` + 시트 B1 비었을 때 GitHub API 폴백

### 자동 업데이트(무비용 강화)
- 다운로드 **최대 3회 재시도**, 타임아웃 300초, 청크 64KB  
- GitHub Release **`MAIL_MONSTER_PRO.exe.sha256`** 과 로컬 해시 비교 후에만 exe 교체  
- 다운로드 직후 **`Unblock-File`**(MOTW 제거), 실패 시 **릴리스 페이지** 열기 + SmartScreen 안내 문구  
- Actions: 빌드 후 `.sha256` 파일 생성·Release에 동시 업로드

### GitHub 전용 업데이트
- `login.py`: 기본 저장소 `GITHUB_RELEASE_REPO_DEFAULT` — 시트 B1 비움 또는 `GITHUB`이면 최신 Release에서 `MAIL_MONSTER_PRO.exe` URL 자동 조회  
- `github_release_repo.txt`(선택)·`MAILMONSTER_GITHUB_REPO`·`MAILMONSTER_DISABLE_GITHUB_RELEASE` 지원  
- `UPDATE_VIA_GITHUB.md`, `github_release_repo.txt.example` 추가

### 저장소·배포
- 루트 `.gitignore` — `config.json`, `credentials.json`, DB 등 비밀·로컬 파일 제외  
- `config.example.json`, `README.md`, `DEPLOY_GITHUB.md`, `scripts/setup_git_and_push.ps1` (SSH 원격 `mail-monster-pro`) 추가
