"""앱/시트 버전 비교. customtkinter 없이 테스트 가능."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple


def strip_invisible_chars(s) -> str:
    if s is None:
        return ""
    t = str(s)
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u200e", "\u200f", "\u2028", "\u2029"):
        t = t.replace(ch, "")
    try:
        t = unicodedata.normalize("NFKC", t)
    except Exception:
        pass
    return t.strip()


def normalize_version_for_compare(s) -> str:
    if s is None:
        return ""
    t = strip_invisible_chars(s).lower()
    t = t.replace("\t", " ").replace("\r", "").replace("\n", " ")
    t = t.strip()
    if len(t) > 1 and t.startswith("v") and (t[1].isdigit() or t[1] == "."):
        t = t[1:].lstrip()
    return t.strip()


def version_numeric_tuple(s) -> Tuple[int, ...]:
    t = normalize_version_for_compare(s)
    if not t:
        return ()
    t = t.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)*)", t)
    if not m:
        return ()
    parts = [p for p in m.group(1).split(".") if p.isdigit()]
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return ()


def versions_effectively_equal(sheet_version, app_version) -> bool:
    cmp = compare_versions(sheet_version, app_version)
    return cmp == 0


def compare_versions(left, right) -> Optional[int]:
    """left < right → -1, 같음 → 0, left > right → 1, 파싱 실패 → None."""
    ta = version_numeric_tuple(left)
    tb = version_numeric_tuple(right)
    if not ta or not tb:
        return None
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def should_prompt_update(remote_version, current_version) -> bool:
    """원격이 현재보다 높을 때만 True. 낮거나 같거나 잘못된 문자열은 False."""
    cmp = compare_versions(remote_version, current_version)
    return cmp == 1


def evaluate_update_prompt(remote_version, current_version) -> Tuple[bool, str]:
    """(업데이트 안내 여부, 사유). 잘못된 문자열은 안내하지 않고 invalid_version."""
    if not str(remote_version or "").strip():
        return False, "empty_remote"
    cmp = compare_versions(remote_version, current_version)
    if cmp is None:
        return False, "invalid_version"
    if cmp == 1:
        return True, "remote_newer"
    if cmp == 0:
        return False, "equal"
    return False, "remote_older"
