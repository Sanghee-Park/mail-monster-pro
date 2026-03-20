# MAIL MONSTER PRO

Windows용 이메일 자동 발송 애플리케이션 (CustomTkinter + SMTP + 구글 시트 연동)

## 요구 사항

- Python 3.10+ (3.12 권장)
- 의존성: `pip install -r requirements.txt`

## 빠른 시작

```bash
python main.py
```

## 설정

1. `config.example.json`을 복사해 `config.json`으로 저장 후 SMTP 계정을 입력합니다.
2. 구글 시트 연동 시 프로젝트 폴더에 `credentials.json`(서비스 계정)을 둡니다.

## 운영 배포

배포·태그·시트 연동 순서는 **[RELEASE.md](RELEASE.md)** 를 따르세요.  
원클릭 로컬 빌드: `.\scripts\package_and_deploy.ps1`

## 업데이트 배포 (GitHub Releases)

구글 드라이브 없이 **GitHub Release**만 쓰는 방법은 [UPDATE_VIA_GITHUB.md](UPDATE_VIA_GITHUB.md)를 참고하세요.

## 빌드

자세한 내용은 [BUILD.md](BUILD.md)를 참고하세요.

```bash
pyinstaller MAIL_MONSTER_PRO.spec --noconfirm
```

## 라이선스

프로젝트 내부 정책에 따릅니다.
