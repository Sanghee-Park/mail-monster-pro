# MAIL MONSTER PRO v2.6.2+ 사용자 프로필 단일화 전환 계획

## 목표
- SMTP 계정(외부메일_1, 지메일_1 등)별 발송자 프로필 구조를 폐기하고, **프로그램 로그인 계정(사용자)** 기준 단일 프로필로 전환한다.
- 모든 SMTP 계정은 같은 로그인 사용자 프로필을 공통 사용한다.
- 기존 기능(깃허브 자동 업데이트, 구글 시트 연동, 중복 차단)은 유지한다.

---

## 정책 정의 (확정)
- 프로필 소유 기준: `self.user_name`(로그인 사용자명)
- 프로필 저장 위치: 별도 파일 `user_profiles.json` (권장) 또는 기존 `config.json` 루트 분리 저장
- 프로필 키: 로그인 사용자 고유키(1차는 `user_name`, 필요 시 추후 `user_id` 확장)
- 발송 치환 소스:
  - `{{내이름}}` / `{{내직책}}` / `{{내전화번호}}` / `{{내이메일}}`
  - 모두 로그인 사용자 프로필에서 읽음
- SMTP 계정 설정(`id`, `pw`, `smtp`, `port`)은 기존대로 계정별 유지

---

## Phase 1: 데이터 모델 전환
- **Task 1-1 (저장소 분리)**
  - `user_profiles.json` 생성/초기화 로직 추가
  - 헬퍼 추가:
    - `load_user_profiles()`
    - `save_user_profiles()`
    - `get_login_user_profile()`
    - `save_login_user_profile()`
- **Task 1-2 (마이그레이션)**
  - 최초 실행 시 기존 `config.json`의 `sender_profile` 중 하나를 탐지해 현재 로그인 사용자 프로필로 1회 이관
  - 이관 후 계정별 `sender_profile`은 읽기 호환만 유지(점진 제거)

---

## Phase 2: UI 전환 (핵심)
- **Task 2-1 (프로필 UI 위치 변경)**
  - [⚙ 계정 설정] 내부의 계정별 프로필 입력 UI를 제거
  - 헤더 또는 별도 팝업에 **[내 프로필]** 버튼 신설
- **Task 2-2 (로그인 사용자 프로필 편집)**
  - 필드: `user_name`, `user_rank`, `user_phone`, `user_email`
  - 저장 대상은 로그인 사용자 단일 프로필
  - 어느 SMTP 계정 화면에서 발송하든 동일 프로필이 사용됨을 명시

---

## Phase 3: 발송 엔진 연동
- **Task 3-1 (치환 함수 소스 교체)**
  - `replace_user_variables()`가 `task_key` 기반이 아닌 로그인 사용자 프로필 기반으로 읽도록 변경
- **Task 3-2 (미리보기/테스트 발송 일치)**
  - `_render_message_with_variables()`, `_open_message_preview()`, `_start_test_send()` 모두 동일 프로필 소스를 사용하도록 통일
- **Task 3-3 (누락값 검증)**
  - 시작 전 누락 항목 경고는 로그인 사용자 프로필 기준으로 유지

---

## Phase 4: 정리 및 호환성
- **Task 4-1 (레거시 호환)**
  - 구버전 `config.json`이 있어도 앱이 정상 동작하도록 fallback 유지
- **Task 4-2 (문서/예시 갱신)**
  - `config.example.json`에서 `sender_profile` 제거
  - `user_profiles.example.json` 추가(선택)
  - `CHANGELOG.md`에 전환 사항 기록

---

## 실행 순서 (이번 작업에서 실제로 할 일)
1. `main_ui.py`에 사용자 프로필 저장/조회 헬퍼 추가 (`user_profiles.json`)
2. 계정 설정 탭의 발송자 정보 UI 제거, 대신 로그인 사용자 프로필 편집 UI(버튼+팝업) 추가
3. `replace_user_variables()` 및 관련 호출부에서 `task_key` 의존 제거
4. 테스트 발송/미리보기/실발송이 모두 로그인 사용자 프로필을 사용하도록 통일
5. 레거시 마이그레이션(기존 `sender_profile` -> 로그인 사용자 프로필) 1회 적용
6. `CHANGELOG.md` 반영
7. 린트/기본 동작 점검 후 보고

---

## 완료 기준 (Definition of Done)
- SMTP 계정을 여러 개 써도 발송자 정보는 로그인 사용자 기준 하나만 관리된다.
- 어떤 SMTP 계정에서 발송해도 `{{내이름}}` 등 치환 결과가 동일하다.
- 기존 사용자 데이터에서 즉시 깨지지 않고 자동 이관 또는 fallback이 동작한다.

