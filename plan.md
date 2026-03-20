# MAIL MONSTER PRO v2.6.1 Patch & User Profile Integration

**[Agent Directive - CRITICAL RULES]**
1. **Task-by-Task Execution**: 하나의 Task가 완료될 때마다 멈추고 보고하세요.
2. **Changelog Generation**: `CHANGELOG.md`에 v2.6.1 패치 내역을 한국어로 기록하세요.
3. **Compatibility**: 기존의 깃허브 자동 업데이트 및 구글 시트 연동 기능이 유지되어야 합니다.

---

## Phase 1: 글로벌 중복 발송 로직 전면 수정 (Template-Centric)
"누가 보냈는가"보다 "어떤 내용을 받았는가"에 집중하여 고객 피로도를 최소화하고 협업 효율을 높인다.

- **Task 1-1 (타 담당자 차단 해제 및 템플릿 기준 통합)** — ✅ **완료 (v2.6.1)**
  - 기존의 '타 담당자가 보낸 이력이 있으면 무조건 차단'하는 규칙을 폐기한다.
  - **[새로운 필터링 규칙]**: `sent_log` 조회 시 담당자(sender)와 상관없이, 수신처(email)와 템플릿명(template_name)이 모두 일치하는 기록이 단 하나라도 있다면 차단한다.
  - 로그 출력: `🚫 스킵: {업체명} (이미 동일 템플릿 발송됨 - 담당자: {sender})`
  - 이외의 경우(담당자가 달라도 템플릿이 다르면)는 모두 발송을 허용한다.

---

## Phase 2: 발송자 프로필 관리 시스템 (Sender Info)
메일 하단에 들어갈 발송자의 상세 정보를 프로그램 내에서 관리하고 동적으로 불러온다.

- **Task 2-1 (상세 프로필 UI)** — ✅ **완료 (v2.6.1)**  
  - [계정 설정] 탭에 '발송자 정보' 그룹박스를 추가하고 4가지 필드를 구현한다.
  - 필드 구성: **이름(`user_name`), 직책(`user_rank`), 전화번호(`user_phone`), 이메일(`user_email`)**
- **Task 2-2 (데이터 바인딩)** — ✅ **완료 (v2.6.1)**  
  - 각 SMTP 계정(Profile)별로 별도의 프로필 정보를 저장할 수 있도록 `config.json` 구조를 확장한다.

## Phase 3: 템플릿 변수 치환 엔진 (Dynamic Tags)
- **Task 3-1 (치환 로직 구현)** — ✅ **완료 (v2.6.1)**
  - 발송 엔진(`real_engine`)이 메일을 보내기 직전, 아래의 태그들을 실제 값으로 치환하는 `replace_user_variables()`를 적용한다.
  - `{{내이름}}` → `user_name`
  - `{{내직책}}` → `user_rank`
  - `{{내전화번호}}` → `user_phone`
  - `{{내이메일}}` → `user_email`
- **Task 3-2 (에디터 가이드)** — ✅ **완료 (v2.6.1)**
  - 메시지 발송 탭에 "내 정보 변수" 안내 라벨을 추가해 사용 가능한 태그를 즉시 확인할 수 있다.

---

