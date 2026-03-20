#!/usr/bin/env python3
"""Task 5-3: GitHub 최신 릴리스의 .exe 에셋 브라우저 다운로드 URL 출력 (수동으로 시트 B1에 붙여넣기용).

사용:
  python scripts/github_latest_release_url.py owner/repo
  set MAILMONSTER_GITHUB_REPO=owner/repo && python scripts/github_latest_release_url.py

환경변수 MAILMONSTER_GITHUB_REPO 가 있으면 인자 생략 가능.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        import requests
    except ImportError:
        print("pip install requests", file=sys.stderr)
        return 1

    repo = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or os.environ.get("MAILMONSTER_GITHUB_REPO", "").strip()
    if not repo or "/" not in repo:
        print("사용법: python github_latest_release_url.py owner/repo", file=sys.stderr)
        return 1

    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        timeout=30,
        headers={"Accept": "application/vnd.github+json"},
    )
    r.raise_for_status()
    data = r.json()
    tag = data.get("tag_name") or ""
    print(f"# tag: {tag}")
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if name.lower().endswith(".exe"):
            print(url)
            return 0
    print("# .exe 에셋 없음", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
