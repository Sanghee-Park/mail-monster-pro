# MAIL MONSTER PRO 변경 이력

## [v2.7.0] - 2026-03-29

### 중복 차단: 로그인 아이디(계정) 기준
- **로컬만 사용할 때**: `sent_history.db`는 **PC마다 따로**라서, **다른 PC 간**에는 이력이 공유되지 않습니다. 같은 PC·같은 DB 안에서는 **로그인 아이디(`account_id`)**로 중복을 판별해, 표시 이름만 같고 아이디가 다른 계정과는 섞이지 않습니다.
- **시트 발송내역을 켠 경우**: 시트에 **F열 `로그인ID`**까지 기록·동기화하여, 다른 PC에서 기동 동기화 후에도 **같은 아이디**면 동일 수신처·동일 템플릿 재발송을 막을 수 있습니다.
- **하위 호환**: `account_id`가 비어 있는 과거 `sent_log` 행은 기존처럼 **발송담당자 표시명(sender)**으로만 매칭합니다.
- **연동**: `login.py` 로그인 성공 시 회원 시트 A열(아이디)을 메인 앱에 전달 → `main.py` → `ModernMailSender(login_user_id=...)`.
- **UI/UX (창 크기 대응)**: 메인 창 `minsize`·상단 헤더 2행 그리드·수신처 Treeview·폼·툴바·팝업 등 리사이즈 시 잘림·버튼 소실 완화. 로그인·회원가입·블랙리스트 창 동일 방향으로 정리.
- **버전 정렬**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.7.0` / `2.7.0`.

## [v2.6.9] - 2026-03-29

### 발송내역 구글 시트 — 기본 비활성(로컬 DB만)
- **목적**: `발송내역` 시트에 행이 쌓이며 시트·앱이 무거워지는 문제 완화. **동일 사용자·동일 템플릿 중복 차단**은 기존과 같이 **로컬 `sent_history.db`의 `sent_log`**만 사용(`check_duplicate_send_status`는 시트를 읽지 않음).
- **기본 동작**: 구글 시트에 **append 하지 않음**, 기동 시 **시트 전체 `get_all_values()` 동기화도 하지 않음**(RAM·네트워크 부담 감소).
- **시트 연동을 다시 켤 때**: 실행 폴더에 `sheet_sent_log_enabled.txt`를 두고 첫 줄에 `1`(또는 `true`/`yes`/`on`/`y`)을 쓰거나, 환경변수 `MAILMONSTER_ENABLE_SHEET_SENT_LOG=1`을 설정.
- **버전 정렬**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.6.9` / `2.6.9`.

## [v2.6.8] - 2026-03-26

### Phase 6 — 지능형 공공기관/단체 필터 (옵션)
- **메시지 발송 탭**: 눈에 띄는 배너 영역에 **「공공기관/단체 필터 적용」** 체크박스 추가(기본 OFF). 켜면 해당 규칙에 맞는 수신처만 스킵.
- **도메인 규칙**: `@` 뒤 도메인이 `.go.kr`, `.or.kr`, `.re.kr`, `.ac.kr`, `.mil.kr` 로 끝나면 스킵.
- **업체명 키워드**: 업체명에 `협회`, `학회`, `조합`, `중앙회`, `공사`, `공단`, `재단` 중 하나가 포함되면 스킵.
- **엔진 연동**: `real_engine`에서 중복 차단 검사 직후·블랙리스트 검사 전에 `check_smart_filter()`로 판별. 스킵 시 **MIME 조립/발송 없음**, `sent_log`·구글 시트 **미기록**, 로그 `🚫 … 필터링: … (공공/단체 규칙 일치로 스킵됨)`.
- **구조 보존**: v2.6.6 이후의 **1건 조립 → 즉시 발송** 루프는 유지(필터는 `continue`로만 분기).
- **버전 정렬**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.6.8` / `2.6.8`.

## [v2.6.7] - 2026-03-26

### 수신처 다중 엑셀 누적 버그 수정
- **다중 파일 선택 지원**: 수신처 업로드에서 `askopenfilenames()`를 사용해 `.xlsx/.xls/.csv` 여러 파일을 한 번에 선택 가능.
- **누적 저장 수정**: `save_recipients_rows` 호출 시 기존 `rows`를 덮어쓰지 않고 `existing_rows + new_rows`로 합쳐 저장.
- **원인 제거**: Tree 목록은 누적되는데 `recipients.json`은 마지막 파일로 덮여 발송 시 마지막 엑셀만 반영되던 불일치 해결.
- **부분 실패 안내**: 일부 파일만 읽기 실패해도 성공 파일은 반영하고, 실패 파일 목록을 경고 팝업으로 안내.
- **정책 보존**: 중복발송 차단 조건(`same_template_same_sender`) 및 발송 엔진의 스킵 정책은 변경 없음.
- **버전 정렬**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.6.7` / `2.6.7`.

## [v2.6.6] - 2026-03-25

### 메모리 최적화 (발송·중복 차단 동작 동일)
- **중복 차단**: v2.6.5 정책 유지 — `check_duplicate_send_status` / `same_template_same_sender` 로직 변경 없음.
- **대량 발송 RAM 절감**: `real_engine`에서 `pre_composed` MIME 누적 제거, 수신처 1건씩 `조립 → 즉시 발송`(스킵·발송 간격·재시도·성공 기록 동일).
- **로그 누적 제한**: `write_log`에서 계정별 로그 박스 최대 1000줄(오래된 줄부터 삭제).
- **엑셀 로드 후 정리**: 수신처 엑셀/CSV 로드 후 `DataFrame` 해제 및 `gc.collect()`.
- **버전 정렬**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.6.6` / `2.6.6`.

## [v2.6.5] - 2026-03-20

### 중복 차단 정책 재정의
- **스킵 조건 변경**: 같은 프로그램 사용자 + 같은 이메일 + 같은 템플릿(`template_key`)일 때만 스킵.
- **타 사용자 이력 허용**: 다른 사용자가 같은 이메일에 같은/유사 템플릿을 보냈어도 스킵하지 않고 발송.
- **공유시트 기록 유지**: 발송 성공 시 `발송내역` 시트 append 및 `sent_log` 기록은 그대로 유지.
- **UI 문구 반영**: 메시지 탭 안내를 새 정책(동일 사용자 기준 중복 차단)으로 변경.
- **버전 정렬**: `login.py` / `main_ui.py` / Inno `MyAppVersion` → `v2.6.5` / `2.6.5`.

## [v2.6.4] - 2026-03-20

### 발송 정책 변경
- **중복 차단 비활성화**: 기존의 동일 이메일/동일 템플릿 스킵 로직을 발송 엔진에서 제거.
- **공유시트 발송내역 유지**: 발송 성공 시 `발송내역` 시트 append 및 로컬 `sent_log` 기록은 그대로 유지.
- **UI 안내 변경**: 메시지 발송 탭 문구를 "중복 차단 비활성화: 공유시트 발송내역만 기록합니다."로 수정.
- **버전 정렬**: `login.py` / `main_ui.py` / Inno `MyAppVersion` → `v2.6.4` / `2.6.4`.

## [v2.6.3] - 2026-03-20

### 긴급 핫픽스
- **테스트 발송 오류 수정**: `cannot access local variable 'sender_name' where it is not associated with a value` 스코프 오류를 해결.
- `_start_test_send()` 내부에서 `sender_name` 재할당을 제거하고 `resolved_sender_name`로 분리해 안전하게 발신자명을 계산.
- 발신자명 폴백(입력값 → 로그인 사용자 프로필 이름 → 로그인 사용자명) 동작을 유지.

## [v2.6.2] - 2026-03-20

### 로그인 사용자 단일 프로필 전환
- **발송자 정보 기준 변경**: SMTP 계정별 `sender_profile` 대신, 로그인 사용자 기준 단일 프로필(`user_profiles.json`)을 사용.
- **내 프로필 UI 추가**: 헤더의 `👤 내 프로필` 버튼 및 팝업으로 이름/직책/전화/이메일을 한 번에 관리.
- **발송 일관성 유지**: 실제 발송/테스트 발송/미리보기의 `{{내이름}}` 계열 치환이 모두 로그인 사용자 프로필을 참조.
- **레거시 마이그레이션**: 기존 `config.json` 계정별 `sender_profile`이 있으면 로그인 사용자 프로필로 1회 자동 이관.
- **업데이트 안정화(한글 경로)**: 업데이트 교체기를 `cmd(.bat)`에서 `PowerShell(.ps1, -LiteralPath)` 방식으로 변경해 경로 깨짐 문제 수정.
- **버전 정렬**: `login.py` / `main_ui.py` / Inno `MyAppVersion` → `v2.6.2` / `2.6.2`.

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
- **업데이트 런처 안정화(한글 경로)**: `_update_runner.bat`(cmd) 방식에서 `_update_runner.ps1`(PowerShell `-LiteralPath`) 방식으로 변경해 한글 경로 깨짐으로 `MAIL_MONSTER_PRO.exe`를 찾지 못하던 문제를 수정.

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
