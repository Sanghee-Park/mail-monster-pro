# MAIL MONSTER PRO 계획 (v2.6.5 중복 정책 · v2.6.7 운영 안정화 릴리스)

## 요구사항(확정)
- **스킵 조건**: 같은 프로그램 사용자(로그인 사용자) + 같은 수신 이메일 + 같은 템플릿일 때만 스킵.
- **허용 조건**: 다른 프로그램 사용자가 같은 수신 이메일에 같은/유사 템플릿을 보낸 경우는 스킵하지 않고 발송.
- 공유시트 `발송내역` 기록은 계속 유지.

---

## 변경 금지 원칙 (가장 중요)
- **중복발송 차단조건은 제품 핵심 정책**이므로, 이번 최적화/개선 작업에서 아래 항목은 **수정 금지**로 둔다.
  - `check_duplicate_send_status()`의 **조건식/리턴값/사유 문자열**(특히 `same_template_same_sender`)
  - `real_engine()`에서 `prevent_dup` 분기 및 “스킵 판단”의 **의미**
  - “다른 사용자 이력은 스킵 사유가 아님(발송 허용)” 정책
- 최적화는 **메모리/성능 이슈가 있는 구현 방식만** 바꾸고, “발송 여부/발송량/스킵 여부” 결과는 동일해야 한다.

---

## 중복 정책 정의 (v2.6.5)
- 중복 키: `(sender_user, email_normalized, template_key_normalized)`
- `sender_user`: 현재 로그인 사용자 식별값(우선 `self.user_name`, 향후 user_id 확장 가능)
- `email_normalized`: `strip + lower`
- `template_key_normalized`: `_dedup_template_key(template_name, title)` 결과에 `strip + lower`
- "유사 템플릿" 개념(부분일치/문장 유사도)은 **중복 차단에 사용하지 않음** (정확 일치만 차단)

---

## Phase 1: 엔진 로직 수정
- **Task 1-1 (check_duplicate_send_status 재구성)**
  - 기존 `email + template` 전역 스킵 로직 제거
  - `email + template + sender_user` 일치 시에만 `same_template_same_sender` 반환
- **Task 1-2 (real_engine 스킵 조건 수정)**
  - 중복 차단 옵션이 켜져 있을 때도, 위 정확 조건에서만 스킵
  - 다른 사용자 발송 이력은 참고만 하고 스킵하지 않음

---

## Phase 2: 기록/동기화 정합성
- **Task 2-1 (sender 저장 보강)**
  - `record_success_to_db`의 `sender` 값이 항상 로그인 사용자로 남도록 점검
  - 빈 sender 레거시 행은 "참고용" 처리(차단 근거로 사용하지 않음)
- **Task 2-2 (공유시트 append 유지)**
  - 기존 `[보낸 날짜, 발송담당자, 업체명, 이메일, 템플릿명]` 구조 유지
  - 중복 차단 정책 변경과 무관하게 기록 동작 보장

---

## Phase 3: UI/문구 정리
- **Task 3-1 (중복 차단 안내 문구 수정)**
  - 메시지 탭 설명을 정책에 맞게 명확화:
  - `같은 사용자 + 같은 이메일 + 같은 템플릿만 스킵`
- **Task 3-2 (로그 메시지 명확화)**
  - 스킵 로그에 `사용자 기준 중복`임을 명시
  - 다른 사용자 이력은 스킵 사유로 출력하지 않음

---

## Phase 4: 메모리 최적화 (발송 로직 결과 불변) — v2.6.6 릴리스에 반영 완료
목표: 발송 성공/실패/스킵 결과(특히 중복 차단)가 바뀌지 않도록 유지하면서, 대량 발송·대용량 첨부에서 RAM 급증을 줄인다.

- **Task 4-1 (Pre-Compose 제거: 1건씩 조립→즉시 발송)** ✅ `main_ui.py` `real_engine`
  - `pre_composed` 제거, 수신처 1건마다 `MIME 조립 → _send_with_retry → 기록`
  - **불변**: 중복 차단/블랙리스트/변수 치환/재시도/성공 기록·발송 간격(연속 실제 발송 사이) 동일
- **Task 4-2 (UI 로그 누적 제한)** ✅ `main_ui.py` `write_log`, 상수 `LOG_CONSOLE_MAX_LINES = 1000`
  - **불변**: 발송 로직 무관(표시만 trim)
- **Task 4-3 (엑셀 로드 메모리 정리)** ✅ `main_ui.py` `load_excel` 내부
  - `save_recipients_rows` 후 `del df`, `gc.collect()`
  - **불변**: 저장 `rows`/Tree 표시 동일

---

## Phase 5: 수신처 다중 엑셀 누적 버그 (v2.6.7) — 반영 완료
목표: 수신처에서 여러 엑셀/CSV를 추가해도 `recipients.json`과 발송 대상이 1:1로 일치하도록 보장한다.

- **Task 5-1 (다중 파일 선택)** ✅ `main_ui.py` `load_excel`
  - 파일 선택을 `askopenfilename` → `askopenfilenames`로 변경
- **Task 5-2 (누적 저장 로직 수정)** ✅
  - 기존 상태(`existing_rows`)를 읽은 뒤 신규 로드(`new_rows`)를 합쳐 `combined_rows` 저장
  - 헤더는 파일 간 병합(`merged_headers`)
- **Task 5-3 (부분 실패 허용/안내)** ✅
  - 일부 파일 로드 실패 시에도 성공 파일은 반영하고, 실패 목록은 경고 팝업으로 안내
- **Task 5-4 (검증 결과)** ✅
  - 코드 경로 검증: 발송 엔진은 `load_recipients_state(...).rows`를 사용
  - 누적 저장 전환으로 “마지막 파일만 발송” 원인 제거 확인

---

## 추가 픽스 제안 (같이 반영 권장)
- **Fix A: 사용자 식별 안정화**
  - 현재 `self.user_name` 충돌 가능성(동명이인)이 있으면, 추후 user_id 열을 추가할 수 있게 함수 시그니처 확장 포인트 확보
- **Fix B: 레거시 데이터 안전 처리**
  - `sender` 비어 있는 과거 이력은 중복 차단에서 제외하여 오탐 차단
- **Fix C: 테스트 케이스**
  - 아래 4가지 시나리오를 수동 검증 체크리스트로 추가
    1) 같은 사용자+같은 이메일+같은 템플릿 => 스킵
    2) 같은 사용자+같은 이메일+다른 템플릿 => 발송
    3) 다른 사용자+같은 이메일+같은 템플릿 => 발송
    4) 다른 사용자+같은 이메일+유사 템플릿 => 발송

---

## 실행 순서 (이번 픽스 작업)
1. `main_ui.py`의 `check_duplicate_send_status`를 사용자 기준 정확 일치 스킵으로 변경
2. `real_engine`의 스킵 분기/로그 문구를 새 정책으로 정리
3. `record_success_to_db` sender 저장값 점검 및 fallback 보강
4. 메시지 탭 안내 문구 업데이트
5. `CHANGELOG.md`에 `v2.6.5` 항목 추가
6. 린트/기본 동작 점검
7. Phase 4(메모리 최적화) 적용: Task 4-1 → 4-2 → 4-3
8. 린트/기본 동작 점검(중복 차단 시나리오 포함)
9. 패키징 및 배포(버전 정책에 맞춰 버전 bump 시)

---

## 완료 기준 (Definition of Done)
- 같은 사용자 동일 템플릿만 스킵되고, 다른 사용자의 동일/유사 템플릿 이력은 발송을 막지 않는다.
- 공유시트 발송내역은 기존대로 누락 없이 기록된다.
- UI 문구와 로그 사유가 정책과 1:1로 일치한다.

