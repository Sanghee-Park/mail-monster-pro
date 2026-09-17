# MAIL MONSTER PRO

Windows용 이메일 자동 발송 애플리케이션 (CustomTkinter + SMTP + 구글 시트 연동)

현재 버전: **v2.8.0**

## 요구 사항

- Python 3.10+ (3.12 권장)
- 의존성: `pip install -r requirements.txt`

## 빠른 시작

```bash
python main.py
```

PyInstaller로 만든 EXE는 작업 디렉터리와 관계없이 실행 파일 위치(`sys.executable`)와, 쓰기가 막힌 경우 `%LOCALAPPDATA%\MAIL_MONSTER_PRO`를 사용합니다. 개발 실행은 `__file__` 기준입니다.

Inno Setup 기본 설치 폴더(`Program Files\MAIL MONSTER PRO`)가 쓰기 불가능하면, v2.7.3에서 실행 파일 옆에 있던 `sent_history.db`, `login_settings.json`, `config.json`, `recipients.json`, `templates.json`, `user_profiles.json`, `extra_holidays.json`을 사용자 폴더로 **1회 복사**합니다. 원본은 삭제하지 않고, 대상에 같은 파일이 있으면 덮어쓰지 않습니다. 데스크톱 등 쓰기 가능한 포터블 폴더는 이동하지 않습니다.

## 설정

1. `config.example.json`을 복사해 `config.json`으로 저장 후 SMTP 계정을 입력합니다.
2. 구글 시트 연동 시 설치 폴더에 `credentials.json`(서비스 계정)을 둡니다.
3. 로그인 화면에서 **자동 로그인**을 켜 두면, Windows 재부팅 후 예약 발송을 이어서 진행할 수 있습니다.

## 발송 정책 (v2.8.0)

- **영업일·시간**: 대한민국 표준시(KST) 기준 월~금, **09:00 정각부터 18:00 직전**까지. 주말·법정공휴일·대체공휴일·근로자의 날·임시공휴일은 발송하지 않습니다.
- **예약 대기**: 업무시간 외에 시작해도 오류로 끝나지 않고 `scheduled_pause`로 저장됩니다. 다음 영업일 09:00에 자동 재개합니다.
- **사용자 정지**: 중지 버튼은 `user_stopped`입니다. 다음 영업일에 자동 재개되지 않습니다. 다시 보내려면 시작을 눌러야 합니다.
- **자동복구**: 진행 중·예약 대기·확인 필요 작업이 있을 때만 현재 사용자 `HKCU\...\Run`에 등록합니다. **Windows에 로그온한 뒤**에만 실행됩니다(잠금 화면·다른 사용자 세션에서는 동작하지 않음).
- **단일 캠페인**: 로그인 사용자당 활성 캠페인(대기/실행/예약/확인 필요)은 하나뿐입니다.

## 임시공휴일 (`extra_holidays.json`)

1. 저장소의 `extra_holidays.example.json`은 **읽기 전용 예시**입니다. 프로그램이 이 파일을 수정하지 않습니다.
2. 실제 목록은 쓰기 가능한 `extra_holidays.json`입니다.
   - 설치 폴더에 쓸 수 있으면 그 위치에 생성됩니다.
   - Program Files처럼 쓰기가 막히면 `%LOCALAPPDATA%\MAIL_MONSTER_PRO\extra_holidays.json`을 사용합니다.
3. 파일이 없으면 프로그램이 빈 `dates` 배열로 생성합니다. 예시 파일을 복사해도 됩니다.

형식:

```json
{
  "dates": ["2026-10-02"],
  "notes": { "2026-10-02": "임시공휴일" }
}
```

날짜는 `YYYY-MM-DD`입니다.

## 제한 사항 (SMTP 정확히 한 번 발송)

SMTP는 서버가 메일을 접수한 뒤 프로그램이 `sent`를 기록하기 전에 종료되면 **exactly-once를 보장할 수 없습니다.**

복구 정책:

- 발송 전에 대기열을 `sending`으로 바꾸고 안정적인 `Message-ID`를 기록합니다.
- 재실행 시 `sent_log`에서 같은 Message-ID가 확인되면 `sent`로 확정합니다.
- 확인되지 않으면 **자동 재발송하지 않고** `needs_review` / `needs_attention`으로 사용자 확인을 요청합니다.
- 사용자는 항목별로 **발송 완료로 처리**, **다시 발송**(중복 경고 후 해당 건만), **건너뛰기**, **캠페인 취소**를 선택할 수 있습니다. 첨부 누락 시 경로를 보여 주고 파일을 다시 지정한 뒤에만 재개합니다.
- 따라서 미확인 건은 수동으로 수신함을 확인한 뒤 처리해야 합니다.

캠페인 DB에는 SMTP 비밀번호를 저장하지 않습니다. 발송 시 `config.json`의 계정(`task_key`)에서 자격증명을 읽습니다. 계정이 없거나 첨부파일이 없으면 캠페인을 무한 재시도하지 않고 중단합니다.

## 운영 배포

배포·태그·시트 연동 순서는 **[RELEASE.md](RELEASE.md)** 를 따르세요.  
원클릭 로컬 빌드: `.\scripts\package_and_deploy.ps1`

GitHub Actions는 **단위 테스트를 먼저 실행**하고, 실패하면 PyInstaller와 Release를 만들지 않습니다. 테스트는 실제 SMTP·실제 HKCU Run 등록·실제 구글 시트 변경을 하지 않습니다.

## 업데이트 배포 (GitHub Releases)

구글 드라이브 없이 **GitHub Release**만 쓰는 방법은 [UPDATE_VIA_GITHUB.md](UPDATE_VIA_GITHUB.md)를 참고하세요.

시트 `설정` A1이 로컬보다 **높은** 버전일 때만 업데이트 안내를 띄웁니다. 시트가 v2.7.3이고 앱이 v2.8.0이면 다운그레이드 안내를 하지 않습니다.

## 빌드

자세한 내용은 [BUILD.md](BUILD.md)를 참고하세요.

```bash
python -m unittest discover -s tests -v
pyinstaller MAIL_MONSTER_PRO.spec --noconfirm
```

## 라이선스

프로젝트 내부 정책에 따릅니다.
