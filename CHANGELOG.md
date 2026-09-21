# MAIL MONSTER PRO 변경 이력

## [v2.8.2] - 2026-09-22

### SMTP 계정별 완전 독립 캠페인
- `campaign_jobs`와 `campaign_queue`를 `(login_user_id, task_key)`별 독립 job으로 생성한다. 다른 SMTP 계정은 수신자·대기열·lease·중지·취소·통계를 공유하지 않는다.
- `recipients.json` 원본을 보존하면서 계정별 recipient set을 SQLite로 승계한다. 정규화 이메일 UNIQUE로 같은 파일 재등록과 대소문자·공백 중복을 제거한다.
- v2.8.1 공용 sender-pool에서 원래 계정을 복원할 수 없는 pending queue는 임의 분배하지 않고 `needs_attention / needs_review`로 차단한다.

### 중복·블랙리스트 안전성
- SMTP 직전 `(login_user_id, normalized_email, final_content_hash)` SQLite reservation을 `BEGIN IMMEDIATE`로 획득하여 다른 SMTP 계정과 동시 runner에서도 한 번만 SMTP에 도달한다.
- SMTP 접수 여부가 불명확한 연결 오류는 `needs_review`로 두고 자동 재발송하지 않는다. 성공은 안정적인 Message-ID와 `sent_log`/reservation에 기록한다.
- blacklist를 queue 생성 시점과 SMTP 연결 직전에 모두 검사한다. 차단 항목은 `skipped / blacklist`로 기록하며 SMTP를 호출하지 않는다.

### 계정별 UI 상태
- 버튼을 선택 계정의 `(login_user_id, task_key, job_id, generation)`으로 렌더링한다. 다른 계정 event와 이전 job의 지연 callback은 현재 화면을 바꾸지 못한다.
- `user_stopped`는 `발송 재개`, `cancelled/completed`는 새 시작, `needs_attention`은 해결 버튼 상태로 즉시 전환한다.

## [v2.8.1] - 2026-09-21

### 다중 SMTP 계정 동시 자동발송
- 로그인 사용자당 논리 캠페인은 하나이며, 서로 다른 `task_key` SMTP 계정은 같은 수신자 대기열의 worker로 합류한다.
- 같은 `task_key`의 중복 시작은 계속 차단한다. 비밀번호·표시 이름이 아니라 `task_key`로 계정을 식별한다.
- 수신자는 `BEGIN IMMEDIATE`로 원자 claim 하며, 한 캠페인에서 같은 이메일은 한 번만 발송한다. 계정별로 목록을 복제하지 않는다.
- 계정별 간격·성공/실패·lease·정지/`needs_attention`을 분리한다. 한 계정의 인증 실패가 다른 계정 발송을 막지 않는다.
- 업무시간(KST 평일·영업일 09:00 이상 18:00 미만)은 모든 worker에 공통 적용. 18:00에 전원 `scheduled_pause`, 다음 영업일 09:00에 정상 worker만 재개.
- 계정 단위 정지와 캠페인 전체 취소를 모두 유지. PC 재실행 시 현재 로그인 사용자의 활성 worker만 복구. HKCU Run은 기존처럼 프로그램 항목 하나.
- SMTP 계정 삭제는 해당 `task_key`에 활성 작업이 있을 때만 막는다. 캠페인 DB에 SMTP 비밀번호를 저장하지 않는다.
- 스키마는 `campaign_workers` 및 queue claim 컬럼을 additive migration으로만 추가한다(기존 DB 삭제 없음). WAL·busy timeout 사용.

### GUI 창 미표시 (로그인 root / parent / grab)
- 로그인 `mainloop`가 끝난 뒤에 숨겨진 `LoginApp`을 destroy 하고 메인 창을 연다. 네이티브 파일 선택창의 parent는 살아 있는 `ModernMailSender`이다.
- `UiDialogManager`가 파일·폴더 선택과 `CTkToplevel`을 Tk 메인 스레드에서만 연다. worker는 `after(0)`로만 요청하며 완료를 기다리지 않는다.
- 영구 `-topmost`를 쓰지 않고, 닫을 때 `grab_release`·플래그 초기화·부모 포커스를 복구한다. 엑셀 파싱과 JSON 저장은 백그라운드에서 수행한다.

## [v2.8.0] - 2026-09-17

### 대한민국 영업일 09:00~18:00 자동발송
- **발송 창**: KST 기준 월~금, 09:00 정각부터 18:00 직전까지. 주말·법정공휴일·대체공휴일·근로자의 날·임시공휴일(holidays 패키지 + `extra_holidays.json`) 제외.
- **예약 대기**: 업무시간 외 시작 시 오류로 종료하지 않고 `scheduled_pause`로 저장. 다음 영업일 09:00에 자동 재개. 사용자 정지(`user_stopped`)는 자동 재개하지 않음.
- **SQLite 영속 대기열**: `campaign_jobs` / `campaign_queue`를 기존 `sent_history.db`에 안전 추가. 수신자 스냅샷 사용, `recipients.json`에서 완료 건을 삭제하지 않음.
- **PC 재부팅 복구**: 활성 작업이 있을 때만 `HKCU\...\Run`에 `--resume`으로 등록. 단일 인스턴스 mutex. 자동 로그인이 꺼져 있으면 안내 후 로그인 화면 유지. 데이터 경로는 cwd가 아니라 EXE/`__file__` 및 `%LOCALAPPDATA%\MAIL_MONSTER_PRO`.
- **SMTP 비밀**: 캠페인 스냅샷에 비밀번호를 넣지 않음. 발송 시 `config.json`에서 조회. 자격증명·첨부 누락 시 `needs_attention`으로 중단.
- **비정상 종료**: `sending` 항목은 `sent_log`의 Message-ID로 확인되면 `sent`, 아니면 `needs_review`(자동 재발송 없음). SMTP exactly-once는 불가능.
- **확인 필요 UI**: `needs_review`는 항목별로 발송 완료 처리 / 다시 발송(중복 경고 후 해당 건만 pending) / 건너뛰기(사유) / 캠페인 취소. 첨부가 없으면 경로 표시 후 다시 지정하고, 파일이 있을 때만 재개.
- **v2.7.3 데이터 승계**: 설치 폴더가 읽기 전용이면 `sent_history.db` 등 기존 파일을 `%LOCALAPPDATA%\MAIL_MONSTER_PRO`로 1회 복사(덮어쓰기·원본 삭제 없음). 포터블(쓰기 가능)은 실행 폴더를 그대로 사용.
- **버전**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.8.0` / `2.8.0`. 원격 버전이 더 낮으면 업데이트 안내 없음.

## [v2.7.3] - 2026-06-16

### Phase 9 — Amazon SES 및 분리형 인증 방식 지원
- **인증 방식 선택 UI (Task 9-1)**: [계정 설정]에 `인증 방식` 라디오 버튼 추가.
  - `일반 SMTP (구 버전: 로그인 아이디 = 보내는 주소)` (기본값)
  - `분리형 인증 (신 버전: Amazon SES 등 / 로그인 ID ≠ From)`
  - 분리형 선택 시에만 **[보내는 사람 주소 (From)]** 입력 칸이 동적으로 노출되며, 아이디 라벨/플레이스홀더가 `SMTP 로그인 아이디 (AKIA...)`로 전환.
- **저장·하위호환 (Task 9-2)**: `config.json` 계정 정보에 `auth_type`("standard"/"separated")과 `sender_email` 키 분리 저장. 기존 계정은 `auth_type`이 없으면 자동으로 "standard"로 인식(`dict.get('auth_type','standard')` 폴백)하여 네이버/다음 등 기존 계정 호환성 완전 보장.
- **발송 봉투 분기 (Task 9-3)**: SMTP 로그인(`server.login`)은 항상 로그인 아이디(`config['id']`/`pw`)를 사용. `msg['From']`은 `_resolve_from_address()`로 분기 — standard는 로그인 아이디, separated는 `sender_email`을 발신 주소로 사용. `_build_single_mime`(실발송)·테스트 발송 모두 적용.
- **유효성 검사 (Task 9-4)**: 분리형 인증 선택 시 보내는 사람 주소가 비어 있거나 이메일 형식이 아니면 경고창을 띄우고 저장을 차단.
- **버전**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.7.3` / `2.7.3`.

## [v2.7.2] - 2026-05-06

### Phase 8 — 블랙리스트(수신거부) 필터링 누수 긴급 패치
- **비교 정규화 강제**: 블랙리스트 매칭 시 타겟 이메일/DB 토큰 모두 `strip().lower()`로 정규화하여 공백·대소문자 차이로 인한 누수 차단.
- **도메인 차단 지원**: 블랙리스트 값이 `@spam.com`, `spam.com`, `*.spam.com` 형태여도 `endswith` 규칙으로 차단.
- **발송 루프 순서 보강**: `real_engine`에서 공공기관 필터 다음에 블랙리스트를 독립 `if`로 검사하고, 걸리면 `🚫 스킵: ... (블랙리스트 차단)` 로그 후 즉시 `continue`하여 MIME 조립/발송 차단.
- **진단 로그 강화**: 블랙리스트 스킵 로그에 실제 매칭 토큰/사유를 함께 출력해 운영 점검 용이성 개선.

## [v2.7.1] - 2026-04-21

### Phase 7 — 계정 UX · 첨부/CID · 본문 해시 중복 차단 · 안정화
- **계정 목록**: 별칭(`display_name`), 우클릭/⚙ 관리, 순서(`__account_order__`), 슬롯 삭제, 활성 행 강조. `config.json` 원자적 저장 유지.
- **템플릿 로드**: 첨부·CID 전면 초기화 후 반영; 목록별 `✕` 삭제. `📎 파일`은 기존 목록에 경로 추가(중복 제외).
- **중복 차단**: 로컬 `sent_log.content_hash`(MIME용 HTML의 SHA256) 기준. 시트 발송내역 연동 코드는 변경 없음. 구 `content_hash` 없는 행은 본문 해시 비교에서 제외.
- **Task 7-4**: `templates.json` / `recipients.json` / `user_profiles.json` 저장도 임시 파일+`os.replace`로 원자적 저장.
- **버전**: `login.py` / `main_ui.py` 폴백 / Inno `MyAppVersion` → `v2.7.1` / `2.7.1`.

### v2.7.1 핫픽스 (동일 태그 재배포용 소스 반영)
- **계정 우클릭 메뉴**: `tk_popup` 직후 `Menu.destroy()`로 인해 항목이 먹통이 되던 문제 수정 — `command` 실행 후 지연 파기, `Menu` 부모를 `winfo_toplevel()`로 지정. 이름 변경·순서 이동 후 활성 표시 새로고침.
- **메시지 발송 탭**: `t3` 전체를 `CTkScrollableFrame`으로 감싸 하단 발송 버튼까지 스크롤로 접근 가능. 첨부/CID 목록 영역 기본 높이 축소(약 3줄 분량).

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
