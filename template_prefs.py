"""템플릿 이름과 분리된 안정 ID. 기존 templates.json 항목은 삭제하지 않는다."""
from __future__ import annotations

import uuid
from typing import Optional, Tuple


def ensure_template_ids(templates: dict) -> Tuple[dict, bool]:
    if not isinstance(templates, dict):
        return {}, False
    changed = False
    for item in templates.values():
        if not isinstance(item, dict):
            continue
        if not str(item.get("id") or "").strip():
            item["id"] = uuid.uuid4().hex
            changed = True
    return templates, changed


def find_template_by_id(templates: dict, template_id: str) -> Tuple[str, Optional[dict]]:
    wanted = str(template_id or "").strip()
    if not wanted or not isinstance(templates, dict):
        return "", None
    for name, item in templates.items():
        if isinstance(item, dict) and str(item.get("id") or "").strip() == wanted:
            return str(name), item
    return "", None


def template_id_for_name(templates: dict, name: str) -> str:
    item = templates.get(name) if isinstance(templates, dict) else None
    if not isinstance(item, dict):
        return ""
    return str(item.get("id") or "").strip()
