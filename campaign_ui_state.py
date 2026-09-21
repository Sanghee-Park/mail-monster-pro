"""SMTP 계정별 캠페인 버튼 상태와 지연 UI 이벤트 검증."""
from __future__ import annotations

from campaign_store import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
)


def button_state_for_status(status: str) -> dict:
    status = status or ""
    if status in (JOB_QUEUED, JOB_RUNNING, JOB_SCHEDULED_PAUSE):
        return {
            "start_state": "disabled",
            "start_text": "🚀 자동발송 시작",
            "stop_state": "normal",
            "cancel_state": "normal",
            "fix_visible": False,
        }
    if status == JOB_USER_STOPPED:
        return {
            "start_state": "normal",
            "start_text": "▶ 발송 재개",
            "stop_state": "disabled",
            "cancel_state": "normal",
            "fix_visible": False,
        }
    if status == JOB_NEEDS_ATTENTION:
        return {
            "start_state": "disabled",
            "start_text": "🚀 자동발송 시작",
            "stop_state": "disabled",
            "cancel_state": "normal",
            "fix_visible": True,
        }
    if status in ("needs_review",):
        return {
            "start_state": "disabled",
            "start_text": "🚀 자동발송 시작",
            "stop_state": "disabled",
            "cancel_state": "normal",
            "fix_visible": True,
        }
    if status in (JOB_COMPLETED, JOB_CANCELLED, ""):
        return {
            "start_state": "normal",
            "start_text": "🚀 자동발송 시작",
            "stop_state": "disabled",
            "cancel_state": "disabled",
            "fix_visible": False,
        }
    return {
        "start_state": "normal",
        "start_text": "🚀 자동발송 시작",
        "stop_state": "disabled",
        "cancel_state": "disabled",
        "fix_visible": False,
    }


def event_matches_current(
    *,
    event_task_key: str,
    event_job_id: str,
    event_generation: int,
    current_task_key: str,
    current_job_id: str,
    current_generation: int,
) -> bool:
    return (
        str(event_task_key or "") == str(current_task_key or "")
        and str(event_job_id or "") == str(current_job_id or "")
        and int(event_generation or 0) == int(current_generation or 0)
    )
