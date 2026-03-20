# MAIL MONSTER PRO 변경 이력

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

### 저장소·배포
- 루트 `.gitignore` — `config.json`, `credentials.json`, DB 등 비밀·로컬 파일 제외  
- `config.example.json`, `README.md`, `DEPLOY_GITHUB.md`, `scripts/setup_git_and_push.ps1` (SSH 원격 `mail-monster-pro`) 추가
