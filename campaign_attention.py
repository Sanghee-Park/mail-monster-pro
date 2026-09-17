"""needs_review / needs_attention 사용자 해결. 자동 재발송 없음."""
from __future__ import annotations

from typing import Optional, Tuple

from campaign_attachments import format_missing_files_reason, missing_attachment_paths
from campaign_store import (
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING,
    ITEM_SENDING,
    ITEM_SENT,
    ITEM_SKIPPED,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    CampaignStore,
)

ACTION_MARK_SENT = "mark_sent"
ACTION_RESEND = "resend"
ACTION_SKIP = "skip"
ACTION_CANCEL_JOB = "cancel_job"
ACTION_REBIND_FILES = "rebind_files"
ACTION_REBIND_CIDS = "rebind_cids"

RESEND_WARNING = (
    "이미 SMTP 서버가 메일을 접수했을 수 있습니다. 다시 발송하면 수신자에게 메일이 한 번 더 갈 수 있습니다."
)

ATTENTION_BUTTON_LABELS = {
    ACTION_MARK_SENT: "발송 완료로 처리",
    ACTION_RESEND: "다시 발송",
    ACTION_SKIP: "건너뛰기",
    ACTION_CANCEL_JOB: "캠페인 취소",
    ACTION_REBIND_FILES: "첨부파일 다시 지정",
    ACTION_REBIND_CIDS: "CID 이미지 다시 지정",
}

# 자동 재발송 없음. 아래 버튼만 사용자 확인 후 동작한다.
ATTENTION_REVIEW_ACTIONS = (
    ACTION_MARK_SENT,
    ACTION_RESEND,
    ACTION_SKIP,
)


class ReviewActionError(ValueError):
    pass


def list_review_items(store: CampaignStore, job_id: str):
    return store.list_items_by_status(job_id, ITEM_NEEDS_REVIEW)


def resolve_review_item(
    store: CampaignStore,
    item_id: int,
    action: str,
    *,
    note: str = "",
    now=None,
    resend_confirmed: bool = False,
) -> dict:
    item = store.get_item(item_id)
    if not item or item.get("status") != ITEM_NEEDS_REVIEW:
        raise ReviewActionError("needs_review 상태인 항목만 처리할 수 있습니다.")
    if action == ACTION_MARK_SENT:
        store.mark_item(
            item_id,
            ITEM_SENT,
            error_message=note or "사용자: 발송 완료로 처리",
            now=now,
        )
    elif action == ACTION_RESEND:
        if not resend_confirmed:
            raise ReviewActionError("다시 발송은 중복 발송 경고 확인 후에만 가능합니다.")
        store.mark_item(
            item_id,
            ITEM_PENDING,
            error_message=note or "사용자: 다시 발송(중복 가능)",
            now=now,
            reset_attempts=True,
        )
    elif action == ACTION_SKIP:
        if not (note or "").strip():
            raise ReviewActionError("건너뛰기에는 사유가 필요합니다.")
        store.mark_item(item_id, ITEM_SKIPPED, error_message=note.strip(), now=now)
    else:
        raise ReviewActionError("알 수 없는 동작입니다.")
    job_id = item["job_id"]
    store.refresh_counts(job_id)
    if store.count_by_status(job_id, ITEM_NEEDS_REVIEW) > 0:
        n = store.count_by_status(job_id, ITEM_NEEDS_REVIEW)
        store.set_needs_attention(job_id, f"확인이 필요한 수신자가 {n}건 남아 있습니다.", now=now)
    return store.get_item(item_id) or {}


def cancel_campaign(store: CampaignStore, job_id: str, *, now=None) -> str:
    store.set_status(job_id, JOB_CANCELLED, now=now, clear_runner=True)
    return JOB_CANCELLED


def replace_job_attachments(store: CampaignStore, job_id: str, *, files=None, imgs=None) -> Tuple[dict, list]:
    job = store.get_job(job_id) or {}
    attach = store.job_snapshot_attachments(job)
    if files is not None:
        attach["files"] = list(files)
    if imgs is not None:
        attach["imgs"] = dict(imgs)
    store.update_attachments(job_id, attach)
    missing = missing_attachment_paths(attach)
    return attach, missing


def maybe_release_attention(
    store: CampaignStore,
    job_id: str,
    *,
    send_allowed: bool,
    next_resume_at: Optional[str] = None,
    now=None,
) -> str:
    """needs_review 가 남아 있거나 첨부가 없으면 재개하지 않는다. 자동 재발송 없음."""
    job = store.get_job(job_id) or {}
    st0 = job.get("status") or ""
    if st0 in (JOB_CANCELLED, JOB_COMPLETED, JOB_USER_STOPPED):
        return st0
    if store.count_by_status(job_id, ITEM_NEEDS_REVIEW) > 0:
        n = store.count_by_status(job_id, ITEM_NEEDS_REVIEW)
        store.set_needs_attention(job_id, f"확인이 필요한 수신자가 {n}건 남아 있습니다.", now=now)
        return JOB_NEEDS_ATTENTION
    missing = missing_attachment_paths(store.job_snapshot_attachments(job))
    if missing:
        store.set_needs_attention(job_id, format_missing_files_reason(missing), now=now)
        return JOB_NEEDS_ATTENTION
    pending = store.count_by_status(job_id, ITEM_PENDING)
    sending = store.count_by_status(job_id, ITEM_SENDING)
    if pending == 0 and sending == 0:
        store.set_status(job_id, JOB_COMPLETED, now=now, clear_runner=True, attention_reason="")
        return JOB_COMPLETED
    if send_allowed:
        store.set_status(job_id, JOB_RUNNING, now=now, clear_runner=True, attention_reason="")
        return JOB_RUNNING
    store.set_status(
        job_id,
        JOB_SCHEDULED_PAUSE,
        next_resume_at=next_resume_at,
        now=now,
        clear_runner=True,
        attention_reason="",
    )
    return JOB_SCHEDULED_PAUSE
