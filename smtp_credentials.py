"""SMTP 스냅샷에서 비밀값을 제거하고, 발송 시 config.json에서 자격증명을 조회한다."""
from __future__ import annotations

import json
import os
from typing import Optional

SECRET_KEYS = {
    "pw",
    "password",
    "passwd",
    "pass",
    "secret",
    "api_key",
    "apikey",
    "token",
    "app_password",
    "apppassword",
    "access_key_secret",
    "secret_access_key",
    "smtp_password",
}

PUBLIC_KEYS = (
    "smtp",
    "port",
    "id",
    "auth_type",
    "sender_email",
    "display_name",
    "task_key",
)


def is_secret_key(name: str) -> bool:
    n = str(name or "").strip().lower()
    if n in SECRET_KEYS:
        return True
    if "password" in n or n.endswith("_pw") or n.endswith("-pw"):
        return True
    if n.endswith("_token") or n.endswith("_secret"):
        return True
    return False


def public_smtp_snapshot(config: Optional[dict], task_key: str = "") -> dict:
    src = config if isinstance(config, dict) else {}
    out = {"task_key": task_key or str(src.get("task_key") or "")}
    for key in PUBLIC_KEYS:
        if key == "task_key":
            continue
        if key in src and src[key] is not None:
            if is_secret_key(key):
                continue
            out[key] = src[key]
    return out


def snapshot_contains_secrets(payload) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return False
    if isinstance(payload, dict):
        for k, v in payload.items():
            if is_secret_key(k) and str(v or "").strip():
                return True
            if snapshot_contains_secrets(v):
                return True
        return False
    if isinstance(payload, list):
        return any(snapshot_contains_secrets(x) for x in payload)
    return False


def load_live_smtp_config(config_path: str, task_key: str) -> Optional[dict]:
    if not config_path or not task_key or not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(task_key)
    if not isinstance(entry, dict):
        return None
    pw = str(entry.get("pw") or "").strip()
    login_id = str(entry.get("id") or "").strip()
    smtp = str(entry.get("smtp") or "").strip()
    if not pw or not login_id or not smtp:
        return None
    return dict(entry)


def resolve_smtp_for_send(config_path: str, task_key: str, snapshot: Optional[dict] = None) -> Optional[dict]:
    live = load_live_smtp_config(config_path, task_key)
    if not live:
        return None
    pub = public_smtp_snapshot(snapshot or {}, task_key)
    merged = dict(pub)
    merged.update(live)
    return merged
