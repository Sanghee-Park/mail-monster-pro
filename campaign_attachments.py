"""캠페인 첨부·CID 파일 존재 검사. 누락 시 임의로 첨부 없이 발송하지 않는다."""
from __future__ import annotations

import os
from typing import Dict, List, Optional


def collect_campaign_file_paths(attachments: Optional[dict]) -> List[str]:
    data = attachments if isinstance(attachments, dict) else {}
    paths: List[str] = []
    for p in list(data.get("files") or []):
        s = str(p or "").strip()
        if s:
            paths.append(s)
    imgs = data.get("imgs") if isinstance(data.get("imgs"), dict) else {}
    for p in imgs.values():
        s = str(p or "").strip()
        if s:
            paths.append(s)
    return paths


def missing_attachment_paths(attachments: Optional[dict]) -> List[str]:
    missing: List[str] = []
    seen = set()
    for p in collect_campaign_file_paths(attachments):
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isfile(p):
            missing.append(p)
    return missing


def format_missing_files_reason(missing: List[str]) -> str:
    if not missing:
        return ""
    shown = "\n".join(f"- {p}" for p in missing[:12])
    extra = "" if len(missing) <= 12 else f"\n- … 외 {len(missing) - 12}개"
    return (
        "첨부파일 또는 CID 이미지를 찾을 수 없어 발송을 중단했습니다. "
        "파일이 이동·삭제되었을 수 있습니다. 파일을 다시 지정하거나 작업을 취소하세요.\n"
        f"{shown}{extra}"
    )


def attention_actions_hint() -> str:
    return "해결: 발송 탭에서 첨부/CID 파일을 다시 지정한 뒤 시작하거나, 작업 취소로 대기열을 종료하세요."
