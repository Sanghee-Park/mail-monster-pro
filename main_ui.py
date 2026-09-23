import customtkinter as ctk
from tkinter import ttk, messagebox, Menu
import gc, hashlib, json, os, sys, random, sqlite3, re, base64
from collections import deque
import smtplib, threading, time, mimetypes
import tempfile, webbrowser
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email import encoders
from email.header import Header
from email.utils import formataddr
from PIL import Image, ImageDraw
import pystray

from business_hours import (
    POLICY_TEXT,
    BusinessHours,
    as_kst,
    ensure_extra_holidays_file,
)
from campaign_store import (
    DuplicateActiveCampaignError,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
    RESUME_JOB_STATUSES,
    CampaignStore,
)
from campaign_runtime import CampaignRunner
from campaign_ui_state import button_state_for_status, event_matches_current
from campaign_attachments import format_missing_files_reason, missing_attachment_paths
from campaign_attention import (
    ACTION_MARK_SENT,
    ACTION_RESEND,
    ACTION_SKIP,
    RESEND_WARNING,
    ReviewActionError,
    cancel_campaign,
    list_review_items,
    maybe_release_attention,
    replace_job_attachments,
    resolve_review_item,
)
from autostart import is_autostart_enabled, sync_autostart
from app_paths import (
    bundled_file,
    extra_holidays_example_path,
    extra_holidays_user_path,
    install_dir,
    resolve_state_files,
    writable_file,
)
from smtp_credentials import public_smtp_snapshot, resolve_smtp_for_send, snapshot_contains_secrets
from ui_safe import schedule_on_ui
from ui_dialogs import UiDialogManager, live_ui_parent, is_usable_parent, is_widget_alive
from json_atomic import StorageWriteError, atomic_write_json, read_json_object, update_json_object
from recipient_import import parse_recipient_excel_files, start_excel_import
from template_prefs import ensure_template_ids, find_template_by_id, template_id_for_name

# 블랙리스트 관리 모듈 (Task 5-1)
try:
    from blacklist_manager import BlacklistManager
except ImportError:
    BlacklistManager = None

try:
    import webview
except ImportError:
    webview = None

try:
    import gspread
except ImportError:
    gspread = None

# 구글 시트 블랙리스트 동기화용 (Phase 5, login과 동일 스프레드시트)
BLACKLIST_SHEET_KEY = "1I5cdNtpJYQuzYt0juhOcgbcltTv7wb3BJFI2AnI2Crw"

BLACKLIST_SHEET_KEY = "1I5cdNtpJYQuzYt0juhOcgbcltTv7wb3BJFI2AnI2Crw"

BASE_DIR = install_dir()
STATE_FILES = resolve_state_files()


# Phase 4: 계정별 로그 박스 무한 누적 방지 (발송 로직과 무관)
LOG_CONSOLE_MAX_LINES = 300
UI_RECIPIENT_PAGE_SIZE = 100
ACCOUNT_PROVIDERS = ["네이버", "다음", "지메일", "네이트", "외부메일"]

# Phase 6 (v2.6.8): 공공기관/단체 필터 — 이메일 도메인 또는 업체명 키워드 일치 시 발송 스킵(옵션)
_SMART_FILTER_DOMAIN_SUFFIXES = (".go.kr", ".or.kr", ".re.kr", ".ac.kr", ".mil.kr")
_SMART_FILTER_COMPANY_KEYWORDS = ("협회", "학회", "조합", "중앙회", "공사", "공단", "재단")

# Phase 8 (v2.7.2): config.json 루트 메타 — 사이드바에 나열할 task_key 순서
CONFIG_META_ACCOUNT_ORDER_KEY = "__account_order__"


def check_smart_filter(email, comp_name):
    """공공/단체 규칙에 해당하면 True(스킵 대상)."""
    e = (email or "").strip().lower()
    if "@" in e:
        try:
            _, domain = e.rsplit("@", 1)
            domain = domain.strip()
            for suf in _SMART_FILTER_DOMAIN_SUFFIXES:
                if domain.endswith(suf):
                    return True
        except Exception:
            pass
    comp = str(comp_name or "")
    for kw in _SMART_FILTER_COMPANY_KEYWORDS:
        if kw in comp:
            return True
    return False


def _hash_body_html_sha256(body_html: str) -> str:
    """Task 7-3: MIME에 실리는 HTML 문자열 기준 SHA256(hex). 중복 차단·sent_log.content_hash에 사용."""
    normalized = (body_html or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_EMAIL_EXTRACT_RE = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")


def _extract_emails(text: str):
    """수신처 셀의 잡다한 문자열에서 이메일들을 추출.
    예) '홍길동 <A@EX.com>; b@y.com' -> ['A@EX.com', 'b@y.com']
    """
    s = str(text or "").strip()
    if not s:
        return []
    try:
        return _EMAIL_EXTRACT_RE.findall(s)
    except Exception:
        return []


def _norm_str(v):
    """Phase 8: 비교 전 문자열 정규화(공백/대소문자 이슈 제거)."""
    return str(v or "").strip().lower()


def _is_domain_blacklist_token(token_norm: str):
    """블랙리스트 토큰이 도메인 규칙인지 판별."""
    t = _norm_str(token_norm)
    if not t:
        return False
    if t.startswith("@"):
        return True
    if t.startswith("*."):
        return True
    # 이메일 전체가 아닌 도메인만 적은 케이스(예: spam.com)
    return "@" not in t and "." in t


def _match_blacklist_token(email_norm: str, token_norm: str):
    """Phase 8: 이메일 vs 블랙리스트 토큰 매칭.
    - 정확 이메일 매칭
    - 도메인 규칙(@spam.com / spam.com / *.spam.com) endswith 매칭
    """
    e = _norm_str(email_norm)
    t = _norm_str(token_norm)
    if not e or not t:
        return False
    if "@" not in e:
        return False

    # 정확 이메일 매칭
    if not _is_domain_blacklist_token(t):
        return e == t

    # 도메인 규칙 매칭
    if t.startswith("@"):
        dom = t[1:]
        return e.endswith("@" + dom) or e.endswith("." + dom)
    if t.startswith("*."):
        dom = t[2:]
        return e.endswith("." + dom) or e.endswith("@" + dom)
    # "spam.com" 형태
    return e.endswith("@" + t) or e.endswith("." + t)


def _dedup_template_key(template_name, title_fallback=""):
    """Task 2-2: 중복 검사·sent_log에 기록하는 템플릿 키. 저장된 템플릿명 우선, 없으면 제목. 양쪽 strip()."""
    tn = (template_name or "").strip()
    if tn:
        return tn
    return (title_fallback or "").strip()


def _default_sender_profile_dict():
    """Phase 2: 계정별 발송자 프로필 기본 구조 (config.json sender_profile)."""
    return {
        "user_name": "",
        "user_rank": "",
        "user_phone": "",
        "user_email": "",
    }


def _parse_sender_profile_from_entry(entry):
    """config.json의 계정 항목에서 sender_profile 추출."""
    d = _default_sender_profile_dict()
    if not isinstance(entry, dict):
        return d
    sp = entry.get("sender_profile")
    if not isinstance(sp, dict):
        return d
    for k in d:
        if k in sp and sp[k] is not None:
            d[k] = str(sp[k]).strip()
    return d


CAMPAIGN_STATUS_KO = {
    JOB_QUEUED: "대기",
    JOB_RUNNING: "실행 중",
    JOB_SCHEDULED_PAUSE: "예약 대기",
    JOB_USER_STOPPED: "사용자 정지",
    JOB_NEEDS_ATTENTION: "확인 필요",
    JOB_COMPLETED: "완료",
    JOB_CANCELLED: "작업 취소",
}


class ModernMailSender(ctk.CTk):
    def __init__(self, user_name="사용자", grade="무료권", remaining="0", login_user_id="", autostart_recovery=False):
        super().__init__()
        self.user_name, self.grade, self.remaining = user_name, grade, remaining
        self.login_user_id = (login_user_id or "").strip()
        self.autostart_recovery = bool(autostart_recovery)
        self._closing = False
        self._start_in_flight = {}
        self._engine_locks = {}
        paths = resolve_state_files()
        self.config_file = paths["config.json"]
        self.user_profiles_file = paths["user_profiles.json"]
        self.template_file = paths["templates.json"]
        self.recipients_file = paths["recipients.json"]
        self.db_path = paths["sent_history.db"]
        self.log_consoles, self.stop_flags, self.tree_views, self.progress_labels = {}, {}, {}, {}
        self.campaign_ui, self.campaign_buttons, self.campaign_job_ids = {}, {}, {}
        self.campaign_worker_ids = {}
        self.campaign_generations = {}
        self.campaign_cancel_flags = {}
        self._smtp_account_entries = {}
        self.current_template_name = {}
        extra_path = extra_holidays_user_path()
        try:
            ensure_extra_holidays_file(extra_path)
        except Exception:
            pass
        self.business_hours = BusinessHours(extra_path=extra_path)
        self.campaign_store = None
        self.icon_filename = "pro.ico"
        
        try:
            from login import CURRENT_VERSION
        except ImportError:
            CURRENT_VERSION = "v2.8.4"
        self.title(f"MAIL MONSTER PRO {CURRENT_VERSION}")
        self.geometry("980x686")  # 기본 크기
        self.minsize(800, 520)  # 축소 시 레이아웃 붕괴·버튼 소실 방지
        ctk.set_appearance_mode("dark")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        try:
            import tkinter as _tk

            _tk._default_root = self
        except Exception:
            pass
        self.dialogs = UiDialogManager(
            lambda: self,
            ui_thread_id=threading.get_ident(),
            show_error=lambda msg, tb: self._show_dialog_error(msg, tb),
        )
        
        if not os.path.exists(self.config_file):
            self._atomic_write_json_path(self.config_file, {}, indent=4, ensure_ascii=False)
        if not os.path.exists(self.template_file):
            self._atomic_write_json_path(self.template_file, {}, indent=4, ensure_ascii=False)
        if not os.path.exists(self.user_profiles_file):
            self._atomic_write_json_path(self.user_profiles_file, {}, indent=2, ensure_ascii=False)
        if not os.path.exists(self.recipients_file):
            self._atomic_write_json_path(self.recipients_file, {}, indent=2, ensure_ascii=False)
        self._recovery_started = False
        self._migrate_legacy_sender_profile_once()
        try:
            self.init_db()
            self.campaign_store = CampaignStore(self.db_path)
        except StorageWriteError as exc:
            self.campaign_store = None
            messagebox.showerror("저장 오류", str(exc))
            raise
        self.campaign_store.migrate_recipient_state(
            self.login_user_id,
            self._read_recipients_state_all(),
        )
        self.setup_ui()
        # Phase 3 Task 3-1: (옵션) 구글 시트 '발송내역' → 로컬 DB 동기화 — 기본은 끔(로컬 DB만)
        if self._sheet_sent_log_enabled():
            threading.Thread(target=self._run_startup_sent_log_sync, daemon=True).start()
        self.after(400, self._recover_campaigns_if_any)

    def init_db(self):
        from json_atomic import clear_readonly

        folder = os.path.dirname(os.path.abspath(self.db_path)) or "."
        if os.path.isfile(self.db_path):
            clear_readonly(self.db_path)
        existed = os.path.isfile(self.db_path)
        try:
            con = sqlite3.connect(self.db_path)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower():
                raise StorageWriteError(
                    "발송 기록",
                    folder,
                    preserved=existed,
                    retryable=True,
                    relaunch=True,
                    reason="데이터베이스가 읽기 전용입니다.",
                ) from exc
            raise
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    account_idx INTEGER NOT NULL,
                    comp TEXT NOT NULL,
                    email TEXT NOT NULL,
                    subject TEXT,
                    template_name TEXT,
                    sent_at TEXT NOT NULL,
                    sender TEXT
                )
                """
            )

            # 기존 DB에 template_name 컬럼이 없을 경우 추가
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            if "template_name" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN template_name TEXT")
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            # Phase 3 Task 3-1: 발송담당자 (구글 시트·2중 필터용)
            if "sender" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN sender TEXT")
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            if "account_id" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN account_id TEXT")
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            if "content_hash" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN content_hash TEXT")
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            if "message_id" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN message_id TEXT")
            cols = [c[1] for c in con.execute("PRAGMA table_info(sent_log)").fetchall()]
            if "normalized_email" not in cols:
                con.execute("ALTER TABLE sent_log ADD COLUMN normalized_email TEXT")
            con.execute(
                """
                UPDATE sent_log SET normalized_email=LOWER(TRIM(email))
                WHERE TRIM(COALESCE(normalized_email,''))=''
                """
            )

            con.execute("CREATE INDEX IF NOT EXISTS idx_sent_task_email ON sent_log(task_key, email)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sent_email_template ON sent_log(email, template_name)")
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_account_email_tmpl ON sent_log(account_id, email, template_name)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_account_email_hash ON sent_log(account_id, email, content_hash)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_account_normalized_hash ON sent_log(account_id, normalized_email, content_hash)"
            )

            # 블랙리스트 테이블 (Task 5-1)
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    comp TEXT,
                    reason TEXT,
                    added_at TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_email ON blacklist(email)")
            con.commit()
        except sqlite3.OperationalError as exc:
            if not existed:
                try:
                    con.close()
                except Exception:
                    pass
                try:
                    if os.path.isfile(self.db_path):
                        os.remove(self.db_path)
                except OSError:
                    pass
            if "readonly" in str(exc).lower():
                raise StorageWriteError(
                    "발송 기록",
                    folder,
                    preserved=True,
                    retryable=True,
                    relaunch=True,
                    reason="데이터베이스가 읽기 전용입니다.",
                ) from exc
            raise
        finally:
            try:
                con.close()
            except Exception:
                pass

    def _read_recipients_state_all(self):
        return read_json_object(self.recipients_file)

    def _write_recipients_state_all(self, data):
        atomic_write_json(self.recipients_file, data, indent=2, ensure_ascii=False, kind="수신처 목록")

    def _notify_storage_error(self, exc: StorageWriteError):
        key = self._live_task_key()
        if key:
            provider, idx = self._split_task_key(key)
            self.write_log(provider, idx, "❌ " + str(exc).replace("\n", " "))
        parent = live_ui_parent(self)

        def show():
            messagebox.showerror("저장 오류", str(exc), parent=parent if is_usable_parent(parent) else None)

        schedule_on_ui(self, show)

    def load_recipients_state(self, task_key, *, include_rows=False):
        data = self._read_recipients_state_all()
        state = data.get(task_key) or {}
        # 호환: 과거에 rows만 바로 저장된 경우 대비
        if isinstance(state, list):
            state = {"rows": state, "last_sent": {}, "headers": []}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("rows", [])
        state.setdefault("last_sent", {})
        state.setdefault("headers", [])
        if include_rows and self.campaign_store and self.campaign_store.recipient_state_migrated(self.login_user_id):
            state["rows"] = self.campaign_store.list_account_recipients(self.login_user_id, task_key)
        elif self.campaign_store and self.campaign_store.recipient_state_migrated(self.login_user_id):
            state["rows"] = []
            state["row_count"] = self.campaign_store.count_account_recipients(self.login_user_id, task_key)
        return state

    def save_recipients_rows(self, task_key, rows, headers=None):
        canonical_rows = list(rows or [])
        result_box = {}

        def mutate(data):
            state = data.get(task_key) or {}
            if isinstance(state, list):
                state = {"rows": state, "last_sent": {}, "headers": []}
            if not isinstance(state, dict):
                state = {}
            state.setdefault("last_sent", {})
            if headers:
                state["headers"] = headers
            if self.campaign_store:
                result_box["result"] = self.campaign_store.replace_account_recipients(
                    self.login_user_id,
                    task_key,
                    canonical_rows,
                )
                state["rows"] = []
                state["row_count"] = int((result_box["result"] or {}).get("total") or 0)
            else:
                state["rows"] = canonical_rows
                result_box["result"] = {"total": len(canonical_rows)}
            data[task_key] = state
            if self.campaign_store and self.campaign_store.recipient_state_migrated(self.login_user_id):
                for key, value in list(data.items()):
                    if isinstance(value, dict) and value.get("rows"):
                        copied = dict(value)
                        copied["rows"] = []
                        data[key] = copied

        update_json_object(self.recipients_file, mutate, indent=2, ensure_ascii=False, kind="수신처 목록")
        return result_box.get("result") or {"total": len(canonical_rows)}

    def _show_dialog_error(self, msg, tb=""):
        parent = live_ui_parent(self)
        try:
            messagebox.showerror("오류", str(msg or "오류가 발생했습니다."), parent=parent if is_usable_parent(parent) else None)
        except Exception:
            pass
        if tb:
            try:
                setattr(self, "_last_ui_error", tb)
            except Exception:
                pass

    def _close_managed_window(self, win, key):
        self.dialogs.close_toplevel(win, key=key)

    def pick_folder_dialog(self, **kwargs):
        return self.dialogs.askdirectory(key=kwargs.pop("key", "pick_folder"), **kwargs)

    def _choose_excel_paths(self):
        return self.dialogs.askopenfilenames(
            key="excel_recipients",
            title="수신처 엑셀 파일 선택",
            filetypes=[("Excel Files", "*.xlsx *.xls *.csv")],
        )

    def _on_load_excel_clicked(self, task_key, tree, update_count_label):
        start_no = 1
        try:
            start_no = len(tree.get_children()) + 1
        except Exception:
            start_no = 1

        def parse_and_save(paths):
            parsed = parse_recipient_excel_files(paths)
            parsed["start_no"] = start_no
            if parsed.get("loaded_files"):
                state = self.load_recipients_state(task_key, include_rows=True)
                existing_rows = list(state.get("rows", []))
                merged_headers = list(state.get("headers", []))
                for col in parsed.get("headers") or []:
                    if col not in merged_headers:
                        merged_headers.append(col)
                source_rows = list(parsed.get("rows") or [])
                combined = existing_rows + source_rows
                try:
                    saved = self.save_recipients_rows(task_key, combined, merged_headers)
                except StorageWriteError as exc:
                    parsed["storage_error"] = str(exc)
                    parsed["saved"] = False
                    return parsed
                parsed["saved"] = True
                parsed["source_count"] = len(source_rows)
                parsed["combined_count"] = int((saved or {}).get("total") or 0)
                parsed["duplicate_count"] = int((saved or {}).get("duplicates") or 0)
                parsed["invalid_count"] = int((saved or {}).get("invalid") or 0)
                parsed["blacklist_count"] = self._count_blacklisted_rows(combined)
            return parsed

        generation = int(getattr(self, "_bind_generation", 0) or 0)

        def apply_on_ui(result):
            schedule_on_ui(
                self,
                lambda r=result, key=task_key, gen=generation: self._apply_excel_import_result(key, gen, r),
            )

        return start_excel_import(
            dialogs=self.dialogs,
            parse_fn=parse_and_save,
            apply_on_ui=apply_on_ui,
            key="excel_recipients",
            title="수신처 엑셀 파일 선택",
            filetypes=[("Excel Files", "*.xlsx *.xls *.csv")],
        )

    def _count_blacklisted_rows(self, rows):
        tokens = []
        con = sqlite3.connect(self.db_path)
        try:
            tokens = [_norm_str(r[0]) for r in con.execute("SELECT email FROM blacklist").fetchall()]
        except Exception:
            tokens = []
        finally:
            con.close()
        tokens = [token for token in tokens if token]
        if not tokens:
            return 0
        matched = 0
        seen = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            email = _norm_str(row.get("이메일") or row.get("email"))
            if not email or email in seen:
                continue
            seen.add(email)
            if any(_match_blacklist_token(email, token) for token in tokens):
                matched += 1
        return matched

    def _apply_excel_import_result(self, task_key, generation, result):
        result = result or {}
        if result.get("storage_error"):
            parent = live_ui_parent(self)
            messagebox.showerror(
                "저장 오류",
                str(result.get("storage_error")),
                parent=parent if is_usable_parent(parent) else None,
            )
            return
        current = self._live_task_key() == task_key and int(getattr(self, "_bind_generation", 0) or 0) == int(generation or 0)
        if result.get("error"):
            if current:
                self._show_dialog_error("엑셀 파일을 처리하는 중 오류가 발생했습니다.", result.get("error"))
            return
        if not result.get("loaded_files"):
            if not current:
                return
            detail = "\n".join((result.get("failed_files") or [])[:5])
            parent = live_ui_parent(self)
            messagebox.showerror(
                "불러오기 실패",
                f"선택한 파일을 읽지 못했습니다.\n{detail}",
                parent=parent if is_usable_parent(parent) else None,
            )
            return
        self._import_summaries[task_key] = {
            "source_count": int(result.get("source_count") or 0),
            "stored_total": int(result.get("combined_count") or 0),
            "duplicate_count": int(result.get("duplicate_count") or 0),
            "invalid_count": int(result.get("invalid_count") or 0),
            "blacklist_count": int(result.get("blacklist_count") or 0),
        }
        if current:
            self._recipient_page = 0
            self._render_recipient_page()
        failed_files = result.get("failed_files") or []
        if current and failed_files:
            detail = "\n".join(failed_files[:5])
            parent = live_ui_parent(self)
            messagebox.showwarning(
                "일부 파일 실패",
                f"{result.get('loaded_files')}개 파일은 추가되었고, {len(failed_files)}개 파일은 실패했습니다.\n{detail}",
                parent=parent if is_usable_parent(parent) else None,
            )

    def update_last_sent_state(self, task_key, no, comp, email):
        def mutate(data):
            state = data.get(task_key) or {}
            if isinstance(state, list):
                state = {"rows": state, "last_sent": {}}
            if not isinstance(state, dict):
                state = {}
            state.setdefault("rows", [])
            state["last_sent"] = {
                "no": int(no) if str(no).isdigit() else no,
                "comp": comp,
                "email": email,
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            data[task_key] = state

        try:
            update_json_object(self.recipients_file, mutate, indent=2, ensure_ascii=False, kind="수신처 목록")
        except StorageWriteError as exc:
            self._notify_storage_error(exc)

    def _effective_template_for_log(self, template_name, subject):
        """Task 4-4: template_name이 비어 있으면 제목으로 대체해 DB·시트·중복키가 빈 문자열이 되지 않게 함."""
        t = (template_name or "").strip()
        if t:
            return t
        return ((subject or "").strip()[:500] or "(미지정)")

    def record_success_to_db(self, task_key, provider, account_idx, comp, email, subject, template_name="", content_hash=None, message_id=None):
        # 수신처 불명확/실패/미전송은 저장하지 않음: 성공했을 때만 호출
        if not comp:
            return
        e = (email or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
            return
        tpl = self._effective_template_for_log(template_name, subject)
        sender = (self.user_name or "").strip()
        acc = (self.login_user_id or "").strip()
        ch = (content_hash or "").strip() or None
        mid = (message_id or "").strip() or None
        normalized_email = e.lower()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO sent_log(task_key, provider, account_idx, comp, email, normalized_email, subject, template_name, sent_at, sender, account_id, content_hash, message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_key,
                    provider,
                    int(account_idx),
                    comp,
                    e,
                    normalized_email,
                    subject,
                    tpl,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    sender,
                    acc or None,
                    ch,
                    mid,
                ),
            )
            con.commit()
        finally:
            con.close()

    def check_duplicate_send_status(self, email, content_hash="", template_name=""):
        """v2.7.1 Task 7-3: `content_hash`가 주어지면 동일 계정+동일 이메일+동일 본문(해시)일 때만 스킵.
        `content_hash`가 비어 있으면(구 호출) 기존처럼 template_name만으로 판별.
        DB에 content_hash가 없는 과거 행은 본문 해시 비교에서 제외(템플릿명만으로는 새 본문을 막지 않음).
        반환: (스킵 여부, 사유, 이전_발송자_표시명) — 타 계정 이력은 스킵 아님.
        """
        e = (email or "").strip()
        if not e:
            return False, None, None
        ch_in = (content_hash or "").strip().lower()
        tpl_key = (template_name or "").strip().lower()
        me_id = (self.login_user_id or "").strip().lower()
        me_name = (self.user_name or "").strip().lower()
        if not me_id and not me_name:
            return False, None, None
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(
                "SELECT sender, template_name, account_id, content_hash FROM sent_log WHERE email=? COLLATE NOCASE",
                (e,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        if not rows:
            return False, None, None

        def _same_account(sender, acc_db):
            a = (acc_db or "").strip().lower()
            if me_id:
                if a and a == me_id:
                    return True
                if not a and me_name and (sender or "").strip().lower() == me_name:
                    return True
                return False
            s = (sender or "").strip().lower()
            return bool(s and s == me_name)

        for sender, tmpl_db, acc_db, ch_db in rows:
            if not _same_account(sender, acc_db):
                continue
            prior = (sender or "").strip() or "(미기록)"
            if ch_in:
                ch_row = (ch_db or "").strip().lower()
                if ch_row and ch_row == ch_in:
                    return True, "same_body_same_sender", prior
                continue
            t = (tmpl_db or "").strip().lower()
            if t == tpl_key:
                return True, "same_template_same_sender", prior
        return False, None, None

    def _profile_key_for_login_user(self):
        return (self.user_name or "").strip() or "default_user"

    def load_user_profiles(self):
        try:
            with open(self.user_profiles_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_user_profiles(self, profiles):
        self._atomic_write_json_path(
            self.user_profiles_file,
            profiles if isinstance(profiles, dict) else {},
            indent=2,
            ensure_ascii=False,
        )

    def get_login_user_profile(self):
        profiles = self.load_user_profiles()
        key = self._profile_key_for_login_user()
        d = _default_sender_profile_dict()
        p = profiles.get(key)
        if isinstance(p, dict):
            for k in d:
                if p.get(k) is not None:
                    d[k] = str(p.get(k)).strip()
        if not d.get("user_name"):
            d["user_name"] = (self.user_name or "").strip()
        return d

    def save_login_user_profile(self, profile):
        d = _default_sender_profile_dict()
        if isinstance(profile, dict):
            for k in d:
                if profile.get(k) is not None:
                    d[k] = str(profile.get(k)).strip()
        if not d.get("user_name"):
            d["user_name"] = (self.user_name or "").strip()
        profiles = self.load_user_profiles()
        profiles[self._profile_key_for_login_user()] = d
        self.save_user_profiles(profiles)

    def _migrate_legacy_sender_profile_once(self):
        """구버전 config.json의 계정별 sender_profile을 로그인 사용자 프로필로 1회 이관."""
        current = self.get_login_user_profile()
        if any((current.get(k) or "").strip() for k in ("user_rank", "user_phone", "user_email")):
            return
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        if not isinstance(cfg, dict):
            return
        for key, entry in cfg.items():
            if key == CONFIG_META_ACCOUNT_ORDER_KEY:
                continue
            if not isinstance(entry, dict):
                continue
            sp = _parse_sender_profile_from_entry(entry)
            if any((sp.get(k) or "").strip() for k in ("user_name", "user_rank", "user_phone", "user_email")):
                self.save_login_user_profile(sp)
                return

    def _sheet_sent_log_enabled(self):
        """공유 시트 '발송내역'에 append·기동 시 시트→DB 동기화. 기본 False(로컬 sent_history.db만, 메모리·시트 부담 최소).
        켜기: BASE_DIR/sheet_sent_log_enabled.txt 첫 줄이 1/true/yes/on/y 또는 환경변수 MAILMONSTER_ENABLE_SHEET_SENT_LOG=1
        """
        p = os.path.join(BASE_DIR, "sheet_sent_log_enabled.txt")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    line = (f.readline() or "").strip().lower()
                if line in ("1", "true", "yes", "on", "y"):
                    return True
            except OSError:
                pass
        env = os.environ.get("MAILMONSTER_ENABLE_SHEET_SENT_LOG", "").strip().lower()
        return env in ("1", "true", "yes", "on", "y")

    def _ensure_sent_log_worksheet(self, spreadsheet):
        """Phase 3: 워크시트 '발송내역'이 없으면 헤더와 함께 생성."""
        if gspread is None:
            return None
        try:
            return spreadsheet.worksheet("발송내역")
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title="발송내역", rows=3000, cols=6)
            ws.append_row(["보낸 날짜", "발송담당자", "업체명", "이메일", "템플릿명", "로그인ID"])
            return ws

    def _sync_sent_log_from_sheet(self):
        """Task 3-1: 구글 시트 '발송내역' 전체를 읽어 로컬 sent_log에 없는 행만 삽입."""
        if not self._sheet_sent_log_enabled():
            return 0
        if gspread is None:
            return 0
        cred_path = bundled_file("credentials.json")
        if not os.path.exists(cred_path):
            return 0
        try:
            client = gspread.service_account(filename=cred_path)
            spreadsheet = client.open_by_key(BLACKLIST_SHEET_KEY)
            ws = self._ensure_sent_log_worksheet(spreadsheet)
            if ws is None:
                return 0
            all_values = ws.get_all_values()
        except Exception:
            return 0
        if len(all_values) < 2:
            return 0
        inserted = 0
        con = sqlite3.connect(self.db_path)
        try:
            for row in all_values[1:]:
                if len(row) < 5:
                    continue
                sent_at = (row[0] or "").strip()
                sender = (row[1] or "").strip()
                comp = (row[2] or "").strip()
                email = (row[3] or "").strip()
                tmpl = (row[4] or "").strip()
                acc_sheet = (row[5] or "").strip() if len(row) > 5 else ""
                if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                    continue
                if not sent_at:
                    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = con.execute(
                    """SELECT 1 FROM sent_log WHERE email=? COLLATE NOCASE AND sent_at=?
                       AND TRIM(COALESCE(sender,''))=? AND LOWER(TRIM(COALESCE(template_name,'')))=LOWER(?)
                       AND LOWER(TRIM(COALESCE(account_id,'')))=LOWER(?) LIMIT 1""",
                    (email, sent_at, sender, tmpl, acc_sheet),
                )
                if cur.fetchone():
                    continue
                con.execute(
                    """INSERT INTO sent_log(task_key, provider, account_idx, comp, email, subject, template_name, sent_at, sender, account_id, content_hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "cloud_sync",
                        "sheet",
                        -1,
                        comp,
                        email,
                        "",
                        tmpl,
                        sent_at,
                        sender,
                        acc_sheet or None,
                        None,
                    ),
                )
                inserted += 1
            con.commit()
        finally:
            con.close()
        return inserted

    def _append_cloud_sent_row(self, comp, email, template_name):
        """Task 3-3: 발송 성공 시 구글 시트 '발송내역'에 한 줄 추가."""
        if not self._sheet_sent_log_enabled():
            return
        if gspread is None:
            return
        cred_path = bundled_file("credentials.json")
        if not os.path.exists(cred_path):
            return
        try:
            client = gspread.service_account(filename=cred_path)
            spreadsheet = client.open_by_key(BLACKLIST_SHEET_KEY)
            ws = self._ensure_sent_log_worksheet(spreadsheet)
            if ws is None:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row(
                [
                    now,
                    (self.user_name or "").strip(),
                    comp or "",
                    (email or "").strip(),
                    (template_name or "").strip(),
                    (self.login_user_id or "").strip(),
                ],
                value_input_option="USER_ENTERED",
            )
        except Exception:
            pass

    def _run_startup_sent_log_sync(self):
        """Task 3-1: UI 블로킹 방지를 위해 백그라운드에서 시트→DB 동기화."""
        try:
            n = self._sync_sent_log_from_sheet()
            if n:
                self.after(0, self._update_stats_label)
        except Exception:
            pass

    def _is_blacklisted(self, email):
        """이메일이 블랙리스트에 있는지 확인 (Task 5-1)"""
        ok, _, _ = self._is_blacklisted_detail(email)
        return ok

    def _is_blacklisted_detail(self, email):
        """Phase 8: 블랙리스트 매칭 상세(매칭값/사유).
        비교는 반드시 양쪽 strip().lower() 기준으로 수행.
        """
        e_raw = (email or "").strip()
        if not e_raw:
            return False, None, None
        candidates_raw = _extract_emails(e_raw) or [e_raw]
        candidates = [_norm_str(x) for x in candidates_raw if _norm_str(x)]
        if not candidates:
            return False, None, None
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute("SELECT email, reason FROM blacklist").fetchall()
            for bl_email_raw, bl_reason in rows:
                bl_token = _norm_str(bl_email_raw)
                if not bl_token:
                    continue
                for e in candidates:
                    if _match_blacklist_token(e, bl_token):
                        reason = _norm_str(bl_reason)
                        return True, bl_token, reason
            return False, None, None
        finally:
            con.close()

    def _add_blacklist(self, email, comp="", reason=""):
        """블랙리스트에 이메일 추가 (Task 5-1)"""
        e_raw = (email or "").strip()
        e_list = _extract_emails(e_raw)
        e = _norm_str(e_list[0] if e_list else e_raw)
        if not e:
            return False
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT OR IGNORE INTO blacklist(email, comp, reason, added_at) VALUES(?, ?, ?, ?)",
                (e, comp, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            con.commit()
            return True
        except Exception:
            return False
        finally:
            con.close()

    def _remove_blacklist(self, email):
        """블랙리스트에서 이메일 제거 (Task 5-1)"""
        e_raw = (email or "").strip()
        e_list = _extract_emails(e_raw)
        e = _norm_str(e_list[0] if e_list else e_raw)
        if not e:
            return False
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "DELETE FROM blacklist WHERE LOWER(TRIM(COALESCE(email,'')))=?",
                (e,),
            )
            con.commit()
            return True
        except Exception:
            return False
        finally:
            con.close()

    def _get_blacklist(self):
        """블랙리스트 전체 조회 (Task 5-1)"""
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute("SELECT email, comp, reason, added_at FROM blacklist ORDER BY added_at DESC")
            return cur.fetchall()
        finally:
            con.close()

    def on_closing(self):
        res = messagebox.askyesnocancel("종료 확인", "프로그램을 트레이로 최소화할까요?")
        if res is True:
            self.withdraw()
            threading.Thread(target=self.run_tray, daemon=True).start()
        elif res is False:
            self._closing = True
            self.destroy()
            os._exit(0)

    def run_tray(self):
        try: img = Image.open(os.path.join(BASE_DIR, self.icon_filename))
        except: img = Image.new('RGB', (64, 64), color=(31, 106, 165))
        self.icon = pystray.Icon("MailMonster", img, "MAIL MONSTER PRO", 
                                 pystray.Menu(pystray.MenuItem('열기', self.show_window), 
                                              pystray.MenuItem('완전 종료', self.quit_window)))
        self.icon.run()

    def show_window(self, icon, item): self.icon.stop(); self.after(0, self.deiconify)
    def quit_window(self, icon, item): self.icon.stop(); os._exit(0)

    def _get_today_sent_count(self):
        """오늘 발송된 건수 조회"""
        from datetime import date
        today = date.today().strftime("%Y-%m-%d")
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute("SELECT COUNT(*) FROM sent_log WHERE sent_at LIKE ?", (f"{today}%",))
            return cur.fetchone()[0]
        finally:
            con.close()

    def _get_total_sent_count(self):
        """누적 발송 건수 조회"""
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute("SELECT COUNT(*) FROM sent_log")
            return cur.fetchone()[0]
        finally:
            con.close()

    def _update_stats_label(self):
        """통계 라벨 업데이트"""
        if hasattr(self, 'stats_label'):
            today = self._get_today_sent_count()
            total = self._get_total_sent_count()
            self.after(0, lambda: self.stats_label.configure(text=f"📊 오늘 발송: {today}건 | 누적 발송: {total}건"))

    def _bind_recipients_tree_autosize(self, tree_wrap, tree):
        """수신처 Treeview: 영역 높이에 맞춰 보이는 행 수·열 너비 조정 (창 크기 변경 대응)."""
        style = ttk.Style()
        state = {"aid": None}

        def apply_size():
            try:
                w = max(tree_wrap.winfo_width(), 80)
                h = max(tree_wrap.winfo_height(), 40)
            except Exception:
                return
            try:
                rh = int(float(style.lookup("Treeview", "rowheight") or 24))
            except Exception:
                rh = 24
            rh = max(rh, 18)
            rows = max(5, min(80, (h - 28) // rh))
            try:
                tree.configure(height=rows)
            except Exception:
                pass
            pad = 24
            inner = max(w - pad, 120)
            no_w = max(40, min(56, int(inner * 0.08)))
            comp_w = max(72, int(inner * 0.38))
            em_w = max(96, inner - no_w - comp_w)
            try:
                tree.column("no", width=no_w, stretch=False, minwidth=36)
                tree.column("comp", width=comp_w, stretch=True, minwidth=60)
                tree.column("email", width=em_w, stretch=True, minwidth=80)
            except Exception:
                pass

        def on_configure(event):
            if event.widget is not tree_wrap:
                return
            if state["aid"] is not None:
                try:
                    tree_wrap.after_cancel(state["aid"])
                except Exception:
                    pass
            state["aid"] = tree_wrap.after(60, _debounced)

        def _debounced():
            state["aid"] = None
            apply_size()

        tree_wrap.bind("<Configure>", on_configure, add="+")
        self.after(200, apply_size)

    def setup_ui(self):
        self._font_title = ("맑은 고딕", 14, "bold")
        self._font_body = ("맑은 고딕", 12)
        self._font_small = ("맑은 고딕", 11)
        self._font_sidebar_active = ("맑은 고딕", 11, "bold")
        theme_color = "#8e44ad" if "관리자" in self.grade else ("#27ae60" if self.grade == "무료권" else "#2980b9")
        header = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=0)
        header.pack(fill="x", side="top")
        header.grid_columnconfigure(1, weight=1)

        try:
            from login import CURRENT_VERSION as _ver
        except ImportError:
            _ver = "v2.8.4"

        title_lbl = ctk.CTkLabel(
            header,
            text=f"🚀 MAIL MONSTER PRO {_ver}",
            font=("맑은 고딕", 18, "bold"),
            text_color=theme_color,
        )
        title_lbl.grid(row=0, column=0, padx=(16, 8), pady=(10, 4), sticky="w")

        self.stats_label = ctk.CTkLabel(
            header,
            text="📊 오늘 발송: 0건 | 누적 발송: 0건",
            font=self._font_small,
            text_color="#e74c3c",
        )
        self.stats_label.grid(row=0, column=1, padx=8, pady=(10, 4), sticky="ew")
        self._update_stats_label()

        user_lbl = ctk.CTkLabel(header, text=f"✨ {self.user_name} 님", font=self._font_body)
        user_lbl.grid(row=0, column=2, padx=(8, 16), pady=(10, 4), sticky="e")

        def _run_sync_blacklist():
            def worker():
                ok, result = self._sync_blacklist_from_sheet()
                if ok:
                    self.after(0, lambda: messagebox.showinfo("동기화 완료", f"{result}건의 차단 목록이 동기화되었습니다."))
                else:
                    self.after(0, lambda: messagebox.showerror("동기화 실패", str(result)))

            threading.Thread(target=worker, daemon=True).start()

        action_bar = ctk.CTkFrame(header, fg_color="transparent")
        action_bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(2, 10))
        action_bar.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(action_bar, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            inner,
            text="⚙️ 블랙리스트",
            width=118,
            height=32,
            font=self._font_small,
            command=self._open_blacklist_manager,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            inner,
            text="🔄 차단 목록 최신화",
            width=138,
            height=32,
            font=self._font_small,
            fg_color="#2980b9",
            command=_run_sync_blacklist,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            inner,
            text="👤 내 프로필",
            width=104,
            height=32,
            font=self._font_small,
            fg_color="#8e44ad",
            command=self._open_user_profile_popup,
        ).pack(side="left", padx=(0, 6))
        
        # 테이블 공통 스타일 (가독성)
        style = ttk.Style()
        style.configure("Treeview", font=("맑은 고딕", 11), rowheight=24)
        style.configure("Treeview.Heading", font=("맑은 고딕", 11, "bold"))

        # 본문: [접기/펼치기 토글 | 사이드바 | 메인 영역]
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8, pady=5)

        # 좌측: 토글 버튼 + 사이드바 (확장형)
        self.sidebar_visible = True
        self.toggle_btn = ctk.CTkButton(
            body, text="◀", width=28, height=80, fg_color="#252525",
            font=("맑은 고딕", 14), command=self._toggle_sidebar,
        )
        self.toggle_btn.pack(side="left", fill="y", padx=(0, 0))

        sidebar = ctk.CTkFrame(body, width=220, fg_color="#252525", corner_radius=8)
        sidebar.pack(side="left", fill="y", padx=(4, 8))
        sidebar.pack_propagate(False)
        self.sidebar_frame = sidebar

        ctk.CTkLabel(sidebar, text="📋 계정 목록", font=self._font_title).pack(pady=(12, 2), padx=12, anchor="w")
        ctk.CTkLabel(
            sidebar,
            text="우클릭 또는 ⚙ — 별칭·순서·삭제",
            font=("맑은 고딕", 10),
            text_color="#7f8c8d",
        ).pack(pady=(0, 6), padx=12, anchor="w")
        scrollable = ctk.CTkScrollableFrame(sidebar, fg_color="transparent")
        scrollable.pack(fill="both", expand=True, padx=8, pady=4)
        self.sidebar_scrollable = scrollable

        self.profile_frames = {}
        self.sidebar_buttons = {}
        self.task_key_to_index = {}
        self._account_providers = list(ACCOUNT_PROVIDERS)
        self._detail_ctx = {"task_key": "", "provider": self._account_providers[0], "idx": 1}
        self._compose_drafts = {}
        self._log_buffers = {}
        self._button_modes = {}
        self._bind_generation = 0
        self._recipient_page = 0
        self._unconfigured_visible = []
        self._import_summaries = {}
        self._shared_widgets = {}
        self._refresh_scheduled = False

        main_content = ctk.CTkFrame(body, fg_color="transparent")
        main_content.pack(side="left", fill="both", expand=True)
        self.main_content_frame = main_content
        content_frame = ctk.CTkFrame(main_content, fg_color="transparent")
        content_frame.pack(fill="both", expand=True)
        self.profile_frames["__detail__"] = content_frame
        first_key = self._get_first_display_key()
        if first_key:
            provider, idx = self._split_task_key(first_key)
            self._detail_ctx = {"task_key": first_key, "provider": provider, "idx": idx}
        self.build_account_detail(
            content_frame,
            self._detail_ctx["provider"],
            self._detail_ctx["idx"],
        )
        self._rebuild_sidebar_buttons()
        self.current_profile = first_key or ""
        if first_key:
            self._bind_account_view(first_key)
        self._highlight_active_sidebar()

    def _read_full_config(self):
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _atomic_write_json_path(self, path, data, indent=4, ensure_ascii=False):
        """JSON 원자적 저장. 실패하면 기존 파일은 그대로 둔다."""
        name = os.path.basename(str(path)).lower()
        kind = "수신처 목록" if "recipient" in name else "설정 파일"
        atomic_write_json(path, data, indent=indent, ensure_ascii=ensure_ascii, kind=kind)

    def _atomic_write_config(self, data):
        """config.json 원자적 저장."""
        if not isinstance(data, dict):
            return
        self._atomic_write_json_path(self.config_file, data, indent=4, ensure_ascii=False)

    def _configured_task_key_set(self, config):
        providers = set(getattr(self, "_account_providers", ACCOUNT_PROVIDERS))
        out = set()
        for task_key, ent in (config or {}).items():
            if not task_key or str(task_key).startswith("__") or not isinstance(ent, dict):
                continue
            provider, _idx = self._split_task_key(task_key)
            if provider in providers and str(ent.get("id") or "").strip():
                out.add(task_key)
        return out

    def _get_configured_task_keys(self):
        """config.json에 id가 있는 task_key만 반환. __account_order__가 있으면 그 순서를 우선."""
        config = self._read_full_config()
        configured_set = self._configured_task_key_set(config)
        custom = config.get(CONFIG_META_ACCOUNT_ORDER_KEY)
        result, seen = [], set()
        if isinstance(custom, list):
            for tk in custom:
                if tk in configured_set and tk not in seen:
                    result.append(tk)
                    seen.add(tk)
        rest = sorted(configured_set - seen, key=self._account_sort_key)
        result.extend(rest)
        return result

    def _account_sort_key(self, task_key):
        provider, idx = self._split_task_key(task_key)
        providers = list(getattr(self, "_account_providers", ACCOUNT_PROVIDERS))
        try:
            order = providers.index(provider)
        except ValueError:
            order = len(providers)
        return (order, int(idx or 0), task_key)

    def _sidebar_task_keys(self):
        keys = self._get_configured_task_keys()
        seen = set(keys)
        for task_key in list(getattr(self, "_unconfigured_visible", []) or []):
            if task_key and task_key not in seen:
                keys.append(task_key)
                seen.add(task_key)
        return keys

    def _ellipsis(self, text, limit=28):
        value = str(text or "")
        if len(value) <= limit:
            return value
        return value[: max(1, limit - 1)] + "…"

    def _sidebar_label_text(self, task_key, index_n):
        cfg = self._read_full_config()
        ent = cfg.get(task_key)
        dname = ""
        if isinstance(ent, dict):
            dname = (ent.get("display_name") or "").strip()
            login_id = str(ent.get("id") or "").strip()
        else:
            login_id = ""
        if dname:
            return self._ellipsis(dname, 28)
        if login_id:
            return self._ellipsis(f"계정 {index_n} ({login_id})", 28)
        return f"계정 {index_n}"

    def _safe_destroy_tk_menu(self, menu):
        """tk.Menu는 tk_popup 직후 destroy 하면 command(다이얼로그 등)가 먹통이 될 수 있어 지연 파기."""
        try:
            if menu is not None and menu.winfo_exists():
                menu.destroy()
        except Exception:
            pass

    def _popup_account_context_menu(self, task_key, x_root, y_root):
        # CTk 창의 Tk 계층에 붙임(일부 환경에서 Menu(self)만으로 command 미동작 방지)
        try:
            tk_host = self.winfo_toplevel()
        except Exception:
            tk_host = self
        menu = Menu(tk_host, tearoff=0)

        def _do_rename(tk=task_key):
            self._rename_account_display(tk)

        def _do_up(tk=task_key):
            self._move_account_in_sidebar(tk, -1)

        def _do_down(tk=task_key):
            self._move_account_in_sidebar(tk, 1)

        def _do_delete(tk=task_key):
            self._delete_account_slot(tk)

        menu.add_command(label="표시 이름(별칭) 변경…", command=_do_rename)
        menu.add_command(label="위로 이동", command=_do_up)
        menu.add_command(label="아래로 이동", command=_do_down)
        menu.add_separator()
        menu.add_command(label="이 슬롯 계정 삭제…", command=_do_delete)
        try:
            menu.tk_popup(int(x_root), int(y_root))
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        # command·다이얼로그가 끝난 뒤 파기 (finally 안에서 즉시 destroy 금지)
        self.after(300, lambda m=menu: self._safe_destroy_tk_menu(m))

    def _on_sidebar_account_right_click(self, event, task_key):
        self._popup_account_context_menu(task_key, event.x_root, event.y_root)

    def _open_account_manage_from_button(self, task_key, widget):
        try:
            widget.update_idletasks()
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + max(widget.winfo_height(), 28)
        except Exception:
            x, y = 0, 0
        self._popup_account_context_menu(task_key, x, y)

    def _rename_account_display(self, task_key):
        cfg = self._read_full_config()
        ent = cfg.get(task_key)
        if not isinstance(ent, dict) or not str(ent.get("id") or "").strip():
            messagebox.showinfo("안내", "연동된 계정만 별칭을 설정할 수 있습니다.", parent=self)
            return
        cur = (ent.get("display_name") or "").strip()
        new_name = self.dialogs.askstring(
            "표시 이름",
            "사이드바에 표시할 별칭(비우면 아이디 표기로 복귀):",
            key="rename_account",
            initialvalue=cur,
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if new_name:
            ent["display_name"] = new_name
        else:
            ent.pop("display_name", None)
        cfg[task_key] = ent
        self._atomic_write_config(cfg)
        self._rebuild_sidebar_buttons()
        self._highlight_active_sidebar()

    def _move_account_in_sidebar(self, task_key, delta):
        keys = self._get_configured_task_keys()
        if task_key not in keys:
            return
        i = keys.index(task_key)
        j = i + int(delta)
        if j < 0 or j >= len(keys):
            return
        lst = list(keys)
        lst[i], lst[j] = lst[j], lst[i]
        cfg = self._read_full_config()
        cfg[CONFIG_META_ACCOUNT_ORDER_KEY] = lst
        self._atomic_write_config(cfg)
        self._rebuild_sidebar_buttons()
        self._highlight_active_sidebar()

    def _clear_account_ui_data(self, task_key):
        w = self._smtp_account_entries.get(task_key)
        if isinstance(w, dict):
            for k in ("e_id", "e_pw", "e_smtp", "e_port"):
                ent = w.get(k)
                if ent is not None:
                    try:
                        ent.delete(0, "end")
                    except Exception:
                        pass
        tree = self.tree_views.get(task_key)
        if tree is not None:
            try:
                for item in tree.get_children():
                    tree.delete(item)
            except Exception:
                pass

    def _delete_account_slot(self, task_key):
        cfg = self._read_full_config()
        ent = cfg.get(task_key)
        login_id = ""
        if isinstance(ent, dict):
            login_id = str(ent.get("id") or "").strip()
        label = self._sidebar_label_text(task_key, self.task_key_to_index.get(task_key, 0) or 0)
        if not login_id:
            messagebox.showinfo("안내", "이 슬롯에는 저장된 SMTP 계정이 없습니다.", parent=self)
            return
        if self.campaign_store and self.campaign_store.has_active_job_for_user(self.login_user_id, task_key):
            messagebox.showwarning(
                "계정 삭제 불가",
                "진행 중이거나 예약 대기 중인 자동발송 작업이 있어 계정을 삭제할 수 없습니다.\n"
                "작업을 완료하거나 취소한 뒤 다시 시도해 주세요.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "계정 삭제",
            f"다음 계정 연동을 해제하고 설정을 삭제할까요?\n\n{label}\n({login_id})\n\n이 슬롯의 수신처 저장 데이터도 함께 삭제됩니다.",
            parent=self,
        ):
            return
        if task_key in cfg:
            del cfg[task_key]
        order = cfg.get(CONFIG_META_ACCOUNT_ORDER_KEY)
        if isinstance(order, list):
            cfg[CONFIG_META_ACCOUNT_ORDER_KEY] = [x for x in order if x != task_key]
        self._atomic_write_config(cfg)
        def _drop_recipient(data):
            if task_key in data:
                del data[task_key]

        try:
            update_json_object(self.recipients_file, _drop_recipient, indent=2, ensure_ascii=False, kind="수신처 목록")
        except StorageWriteError as exc:
            self._notify_storage_error(exc)
        self._clear_account_ui_data(task_key)
        self._unconfigured_visible = [key for key in self._unconfigured_visible if key != task_key]
        self._compose_drafts.pop(task_key, None)
        self._log_buffers.pop(task_key, None)
        if self.campaign_store:
            try:
                self.campaign_store.replace_account_recipients(self.login_user_id, task_key, [])
                self.campaign_store.clear_last_template_id(self.login_user_id, task_key)
            except Exception:
                pass
        was_current = getattr(self, "current_profile", None) == task_key
        self._rebuild_sidebar_buttons()
        new_keys = self._sidebar_task_keys()
        cur = getattr(self, "current_profile", None)
        if was_current or (cur and cur not in new_keys):
            if new_keys:
                self._switch_profile(new_keys[0])
            else:
                self._highlight_active_sidebar()
        else:
            self._highlight_active_sidebar()

    def _highlight_active_sidebar(self):
        cur = getattr(self, "current_profile", None)
        active_font = getattr(self, "_font_sidebar_active", ("맑은 고딕", 11, "bold"))
        base_font = getattr(self, "_font_small", ("맑은 고딕", 11))
        previous = getattr(self, "_highlighted_task", None)
        targets = [key for key in (previous, cur) if key]
        buttons = getattr(self, "sidebar_buttons", {})
        gears = getattr(self, "sidebar_gear_buttons", {})
        for tk in targets:
            btn = buttons.get(tk)
            if btn is not None:
                try:
                    if tk == cur:
                        btn.configure(fg_color="#1f538d", font=active_font, text_color="#ffffff")
                    else:
                        btn.configure(fg_color="transparent", font=base_font, text_color=("gray10", "gray90"))
                except Exception:
                    pass
            gear = gears.get(tk)
            if gear is not None:
                try:
                    gear.configure(fg_color="#2c5a8c" if tk == cur else "#333333")
                except Exception:
                    pass
        self._highlighted_task = cur

    def _next_provider_index(self, provider):
        used = set()
        config = self._read_full_config()
        for task_key in list(config.keys()) + list(getattr(self, "_unconfigured_visible", []) or []):
            current, idx = self._split_task_key(task_key)
            if current == provider:
                used.add(int(idx or 0))
        nxt = 1
        while nxt in used:
            nxt += 1
        return nxt

    def _create_account_slot(self, provider):
        provider = provider if provider in self._account_providers else self._account_providers[0]
        task_key = f"{provider}_{self._next_provider_index(provider)}"
        if task_key not in self._unconfigured_visible and task_key not in self._get_configured_task_keys():
            self._unconfigured_visible.append(task_key)
        self._rebuild_sidebar_buttons()
        self._switch_profile(task_key)

    def _get_first_display_key(self):
        """처음 표시할 키: 설정된 계정이 있으면 첫 번째, 없으면 첫 빈 슬롯"""
        configured = self._sidebar_task_keys()
        return configured[0] if configured else ""

    def _rebuild_sidebar_buttons(self):
        """사이드바 버튼을 설정된 계정만 1부터 스택으로 다시 그림"""
        scrollable = getattr(self, "sidebar_scrollable", None)
        if not scrollable:
            return
        for w in scrollable.winfo_children():
            w.destroy()
        configured = self._sidebar_task_keys()
        counts = {}
        if self.campaign_store:
            try:
                counts = self.campaign_store.recipient_counts_by_task(self.login_user_id)
            except Exception:
                counts = {}
        self.task_key_to_index.clear()
        self.sidebar_buttons.clear()
        self.sidebar_gear_buttons = {}
        for n, task_key in enumerate(configured, 1):
            self.task_key_to_index[task_key] = n
            label = self._ellipsis(f"{self._sidebar_label_text(task_key, n)} · {int(counts.get(task_key) or 0)}", 32)
            row = ctk.CTkFrame(scrollable, fg_color="transparent")
            row.pack(fill="x", pady=2)
            btn = ctk.CTkButton(
                row,
                text=label,
                command=lambda tk=task_key: self._switch_profile(tk),
                fg_color="transparent",
                anchor="w",
                height=36,
                font=self._font_small,
            )
            btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
            btn.bind("<Button-3>", lambda e, tk=task_key: self._on_sidebar_account_right_click(e, tk))
            self.sidebar_buttons[task_key] = btn
            gear = ctk.CTkButton(
                row,
                text="⚙",
                width=32,
                height=36,
                fg_color="#333333",
                font=self._font_small,
            )
            gear.configure(command=lambda tk=task_key, g=gear: self._open_account_manage_from_button(tk, g))
            gear.pack(side="right")
            self.sidebar_gear_buttons[task_key] = gear
        add_btn = ctk.CTkButton(
            scrollable, text="➕ 계정 추가",
            fg_color="#333", height=36, font=self._font_small,
            command=self._on_add_account_click,
        )
        add_btn.pack(fill="x", pady=8)
        self.sidebar_add_btn = add_btn
        self._highlight_active_sidebar()

    def _on_add_account_click(self):
        """계정 추가: 공급자를 고르면 다음 번호의 독립 task_key를 만든다."""
        try:
            tk_host = self.winfo_toplevel()
        except Exception:
            tk_host = self
        menu = Menu(tk_host, tearoff=0)
        for provider in self._account_providers:
            menu.add_command(label=provider, command=lambda p=provider: self._create_account_slot(p))
        try:
            widget = getattr(self, "sidebar_add_btn", None)
            if widget is not None:
                widget.update_idletasks()
                x = widget.winfo_rootx()
                y = widget.winfo_rooty() + max(widget.winfo_height(), 28)
            else:
                x, y = 40, 40
            menu.tk_popup(int(x), int(y))
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        self.after(300, lambda m=menu: self._safe_destroy_tk_menu(m))

    def _toggle_sidebar(self):
        """사이드바 접기/펼치기 (확장형)"""
        if self.sidebar_visible:
            self.sidebar_frame.pack_forget()
            self.toggle_btn.configure(text="▶")
            self.sidebar_visible = False
        else:
            self.sidebar_frame.pack(side="left", fill="y", padx=(4, 8), before=self.main_content_frame)
            self.toggle_btn.configure(text="◀")
            self.sidebar_visible = True

    def _update_sidebar_label(self, task_key):
        """사이드바 버튼 텍스트를 설정: 별칭(display_name) 또는 '계정 N (아이디)'"""
        if task_key not in getattr(self, "sidebar_buttons", {}):
            return
        n = self.task_key_to_index.get(task_key, 0)
        label = self._sidebar_label_text(task_key, n)
        self.sidebar_buttons[task_key].configure(text=label)

    def _switch_profile(self, task_key):
        """같은 상세 화면에 선택 계정 데이터만 다시 바인딩한다."""
        if not task_key:
            return
        current = self._live_task_key()
        if current and current != task_key:
            self._save_compose_draft(current)
        self._bind_account_view(task_key)

    def _live_task_key(self):
        return str((getattr(self, "_detail_ctx", None) or {}).get("task_key") or "")

    def _save_compose_draft(self, task_key):
        widgets = getattr(self, "_shared_widgets", None) or {}
        title = widgets.get("title")
        body = widgets.get("body")
        sender = widgets.get("sender")
        if title is None or body is None or sender is None:
            return
        data = widgets.get("data") or {}
        self._compose_drafts[task_key] = {
            "title": title.get(),
            "body": body.get("1.0", "end-1c"),
            "sender": sender.get(),
            "files": list(data.get("files") or []),
            "imgs": dict(data.get("imgs") or {}),
            "interval": widgets["interval"].get() if widgets.get("interval") is not None else "5분",
            "prevent_dup": bool(widgets["prevent_dup"].get()) if widgets.get("prevent_dup") is not None else True,
            "public_filter": bool(widgets["public_filter"].get()) if widgets.get("public_filter") is not None else False,
        }

    def _bind_account_view(self, task_key, restoring=True):
        provider, idx = self._split_task_key(task_key)
        self._detail_ctx = {"task_key": task_key, "provider": provider, "idx": idx}
        self._bind_generation = int(getattr(self, "_bind_generation", 0) or 0) + 1
        self.current_profile = task_key
        self._recipient_page = 0
        widgets = self._shared_widgets or {}
        if widgets:
            self.tree_views = {task_key: widgets["tree"]}
            self.log_consoles = {task_key: widgets["log"]}
            self.campaign_buttons = {task_key: widgets["buttons"]}
            self.campaign_ui = {task_key: widgets["campaign_ui"]}
            self.progress_labels = {task_key: widgets["progress"]}
            self._smtp_account_entries = {task_key: widgets["smtp"]}
        self._load_smtp_fields(task_key)
        if getattr(self, "_provider_label", None) is not None:
            try:
                self._provider_label.configure(text=f"선택 계정: {task_key}")
            except Exception:
                pass
        self._restore_compose(task_key, restoring=restoring)
        self._render_log(task_key)
        self._render_recipient_page()
        self._highlight_active_sidebar()
        self._refresh_campaign_ui(task_key)
        mode = self._button_modes.get(task_key, "")
        self._set_send_buttons(task_key, mode)

    def _load_smtp_fields(self, task_key):
        widgets = (self._shared_widgets or {}).get("smtp") or {}
        config = self._read_full_config().get(task_key)
        config = config if isinstance(config, dict) else {}
        values = {
            "e_id": config.get("id") or "",
            "e_pw": config.get("pw") or "",
            "e_smtp": config.get("smtp") or "",
            "e_port": str(config.get("port") or ""),
            "e_from": config.get("sender_email") or "",
        }
        for name, value in values.items():
            entry = widgets.get(name)
            if entry is None:
                continue
            try:
                entry.delete(0, "end")
                if value:
                    entry.insert(0, str(value))
            except Exception:
                pass
        auth = widgets.get("auth_type_var")
        if auth is not None:
            try:
                auth.set(config.get("auth_type") or "standard")
            except Exception:
                pass

    def _restore_compose(self, task_key, restoring=True):
        widgets = self._shared_widgets or {}
        draft = self._compose_drafts.get(task_key)
        title = widgets.get("title")
        body = widgets.get("body")
        sender = widgets.get("sender")
        data = widgets.get("data")
        if title is None or body is None or sender is None or data is None:
            return
        if draft:
            self._fill_compose(draft)
            return
        if restoring and self._apply_saved_template(task_key):
            return
        self._fill_compose({"title": "", "body": "", "sender": "", "files": [], "imgs": {}, "interval": "5분", "prevent_dup": True, "public_filter": False})

    def _fill_compose(self, draft):
        widgets = self._shared_widgets or {}
        title = widgets.get("title")
        body = widgets.get("body")
        sender = widgets.get("sender")
        data = widgets.get("data")
        try:
            title.delete(0, "end")
            if draft.get("title"):
                title.insert(0, draft.get("title") or "")
            body.delete("1.0", "end")
            if draft.get("body"):
                body.insert("1.0", draft.get("body") or "")
            sender.delete(0, "end")
            if draft.get("sender"):
                sender.insert(0, draft.get("sender") or "")
        except Exception:
            pass
        data["files"] = list(draft.get("files") or [])
        data["imgs"] = dict(draft.get("imgs") or {})
        if widgets.get("interval") is not None:
            try:
                widgets["interval"].set(draft.get("interval") or "5분")
            except Exception:
                pass
        if widgets.get("prevent_dup") is not None:
            widgets["prevent_dup"].set(bool(draft.get("prevent_dup", True)))
        if widgets.get("public_filter") is not None:
            widgets["public_filter"].set(bool(draft.get("public_filter", False)))
        refresh = widgets.get("refresh_attach")
        if callable(refresh):
            refresh()

    def _load_templates(self):
        try:
            with open(self.template_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data, changed = ensure_template_ids(data)
        if changed:
            self._atomic_write_json_path(self.template_file, data, indent=4, ensure_ascii=False)
        return data

    def _remember_template(self, task_key, template_name):
        templates = self._load_templates()
        template_id = template_id_for_name(templates, template_name)
        self.current_template_name[task_key] = template_name or ""
        if self.campaign_store and template_id:
            self.campaign_store.set_last_template_id(self.login_user_id, task_key, template_id)
        elif self.campaign_store:
            self.campaign_store.clear_last_template_id(self.login_user_id, task_key)

    def _apply_saved_template(self, task_key):
        if not self.campaign_store:
            return False
        template_id = self.campaign_store.get_last_template_id(self.login_user_id, task_key)
        if not template_id:
            return False
        name, item = find_template_by_id(self._load_templates(), template_id)
        if not item:
            self.campaign_store.clear_last_template_id(self.login_user_id, task_key)
            self.current_template_name.pop(task_key, None)
            return False
        self.current_template_name[task_key] = name
        self._fill_compose({
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "sender": item.get("sender") or "",
            "files": list(item.get("files") or []),
            "imgs": dict(item.get("imgs") or {}),
            "interval": "5분",
            "prevent_dup": True,
            "public_filter": False,
        })
        return True

    def _render_recipient_page(self):
        widgets = self._shared_widgets or {}
        tree = widgets.get("tree")
        count_lbl = widgets.get("count")
        import_lbl = widgets.get("import")
        page_lbl = widgets.get("page")
        if tree is None:
            return
        task_key = self._live_task_key()
        total = 0
        if self.campaign_store and task_key:
            total = self.campaign_store.count_account_recipients(self.login_user_id, task_key)
        page_size = UI_RECIPIENT_PAGE_SIZE
        pages = max(1, (total + page_size - 1) // page_size) if total else 1
        self._recipient_page = min(max(0, int(self._recipient_page or 0)), pages - 1)
        offset = self._recipient_page * page_size
        rows = []
        if self.campaign_store and task_key and total:
            rows = self.campaign_store.list_account_recipients_page(
                self.login_user_id, task_key, offset, page_size
            )
        try:
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for index, row in enumerate(rows, start=offset + 1):
                tree.insert("", "end", values=(index, row.get("업체명") or row.get("comp") or "", row.get("이메일") or row.get("email") or ""))
        except Exception:
            return
        if count_lbl is not None:
            count_lbl.configure(text="등록된 수신처 없음" if total == 0 else f"전체 {total}건")
        if page_lbl is not None:
            page_lbl.configure(text=f"{self._recipient_page + 1} / {pages}")
        summary = (self._import_summaries or {}).get(task_key) or {}
        if import_lbl is not None:
            if not summary:
                import_lbl.configure(text="")
            else:
                waiting = max(0, int(summary.get("stored_total") or total) - int(summary.get("blacklist_count") or 0))
                import_lbl.configure(
                    text=(
                        f"원본 {int(summary.get('source_count') or 0)} · 유효 {total} · "
                        f"중복 제외 {int(summary.get('duplicate_count') or 0)} · "
                        f"형식 오류 {int(summary.get('invalid_count') or 0)} · "
                        f"블랙리스트 {int(summary.get('blacklist_count') or 0)} · 발송 대기 {waiting}"
                    )
                )

    def _recipient_prev_page(self):
        self._recipient_page = max(0, int(self._recipient_page or 0) - 1)
        self._render_recipient_page()

    def _recipient_next_page(self):
        self._recipient_page = int(self._recipient_page or 0) + 1
        self._render_recipient_page()

    def _render_log(self, task_key):
        box = (self._shared_widgets or {}).get("log")
        if box is None:
            return
        lines = list((self._log_buffers or {}).get(task_key) or [])
        try:
            box.configure(state="normal")
            box.delete("1.0", "end")
            if lines:
                box.insert("end", "\n".join(lines) + "\n")
            box.see("end")
            box.configure(state="disabled")
        except Exception:
            pass

    def _start_test_for_current(self, title_e, body_t, sender_e, cur_d, send_b, test_b):
        task_key = self._live_task_key()
        provider, idx = self._split_task_key(task_key)
        self._start_test_send(
            provider,
            idx,
            title_e.get(),
            body_t.get("1.0", "end-1c"),
            sender_e.get(),
            {"files": list(cur_d.get("files") or []), "imgs": dict(cur_d.get("imgs") or {})},
            send_b,
            test_b,
        )

    def _open_user_profile_popup(self):
        """로그인 사용자 단일 프로필 편집 팝업."""
        key = "user_profile"
        pop = self.dialogs.open_toplevel(key, title="내 프로필", geometry="430x420", minsize=(320, 380), modal=False)
        if pop is None:
            return
        if getattr(pop, "_ui_dialog_built", False):
            return
        pop._ui_dialog_built = True

        profile = self.get_login_user_profile()
        ctk.CTkLabel(pop, text=f"로그인 사용자: {self.user_name}", font=self._font_small, text_color="#95a5a6").pack(anchor="w", padx=16, pady=(14, 8))
        ctk.CTkLabel(pop, text="발송자 정보", font=self._font_title).pack(anchor="w", padx=16, pady=(0, 8))
        _e = {"height": 36, "font": self._font_body}
        e_name = ctk.CTkEntry(pop, placeholder_text="이름 (user_name)", **_e); e_name.pack(fill="x", padx=16, pady=5)
        e_rank = ctk.CTkEntry(pop, placeholder_text="직책 (user_rank)", **_e); e_rank.pack(fill="x", padx=16, pady=5)
        e_phone = ctk.CTkEntry(pop, placeholder_text="전화번호 (user_phone)", **_e); e_phone.pack(fill="x", padx=16, pady=5)
        e_email = ctk.CTkEntry(pop, placeholder_text="이메일 (user_email)", **_e); e_email.pack(fill="x", padx=16, pady=5)
        if profile.get("user_name"): e_name.insert(0, profile.get("user_name", ""))
        if profile.get("user_rank"): e_rank.insert(0, profile.get("user_rank", ""))
        if profile.get("user_phone"): e_phone.insert(0, profile.get("user_phone", ""))
        if profile.get("user_email"): e_email.insert(0, profile.get("user_email", ""))

        ctk.CTkLabel(
            pop,
            text="모든 SMTP 계정에서 동일한 내 프로필이 공통 사용됩니다.",
            font=("맑은 고딕", 10),
            text_color="#95a5a6",
        ).pack(anchor="w", padx=16, pady=(8, 10))

        def _save():
            data = {
                "user_name": e_name.get().strip(),
                "user_rank": e_rank.get().strip(),
                "user_phone": e_phone.get().strip(),
                "user_email": e_email.get().strip(),
            }
            self.save_login_user_profile(data)
            messagebox.showinfo("저장 완료", "내 프로필이 저장되었습니다.", parent=pop)
            self._close_managed_window(pop, key)

        btn_row = ctk.CTkFrame(pop, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(2, 8))
        ctk.CTkButton(btn_row, text="저장", width=120, fg_color="#28a745", command=_save).pack(side="left")
        ctk.CTkButton(btn_row, text="닫기", width=120, command=lambda: self._close_managed_window(pop, key)).pack(side="right")

    def build_account_detail(self, parent, provider, idx):
        task_key = f"{provider}_{idx}"
        func_tabs = ctk.CTkTabview(parent, segmented_button_fg_color="#333333")
        func_tabs.pack(fill="both", expand=True, padx=2, pady=2)
        t1, t2, t3 = func_tabs.add("⚙ 계정 설정"), func_tabs.add("👥 수신처"), func_tabs.add("📝 메시지 발송")

        t1_scroll = ctk.CTkScrollableFrame(t1, fg_color="transparent")
        t1_scroll.pack(fill="both", expand=True, padx=8, pady=8)

        setup_box = ctk.CTkFrame(t1_scroll, fg_color="transparent")
        setup_box.pack(fill="x", expand=True)
        _e = {"height": 38, "font": self._font_body}
        ctk.CTkLabel(setup_box, text="SMTP 계정", font=self._font_title).pack(anchor="w", pady=(0, 4))
        provider_lbl = ctk.CTkLabel(setup_box, text="", font=self._font_small, text_color="#95a5a6")
        provider_lbl.pack(anchor="w", pady=(0, 6))
        self._provider_label = provider_lbl

        # Phase 9 (v2.7.3): 인증 방식 선택 — 일반 SMTP(구버전) / 분리형 인증(Amazon SES 등)
        auth_type_var = ctk.StringVar(value="standard")
        auth_box = ctk.CTkFrame(setup_box, fg_color="#252525", corner_radius=8)
        auth_box.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(auth_box, text="인증 방식", font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        ctk.CTkRadioButton(
            auth_box,
            text="일반 SMTP (구 버전: 로그인 아이디 = 보내는 주소)",
            variable=auth_type_var,
            value="standard",
            font=("맑은 고딕", 11),
            command=lambda: _on_auth_type_change(),
        ).pack(anchor="w", padx=12, pady=2)
        ctk.CTkRadioButton(
            auth_box,
            text="분리형 인증 (신 버전: Amazon SES 등 / 로그인 ID ≠ From)",
            variable=auth_type_var,
            value="separated",
            font=("맑은 고딕", 11),
            command=lambda: _on_auth_type_change(),
        ).pack(anchor="w", padx=12, pady=(2, 8))

        id_label = ctk.CTkLabel(setup_box, text="아이디", font=("맑은 고딕", 10), text_color="#95a5a6")
        id_label.pack(anchor="w", pady=(0, 0))
        e_id = ctk.CTkEntry(setup_box, placeholder_text="아이디", **_e)
        e_id.pack(fill="x", pady=5)
        e_pw = ctk.CTkEntry(setup_box, placeholder_text="앱 비밀번호", show="*", **_e)
        e_pw.pack(fill="x", pady=5)
        e_smtp = ctk.CTkEntry(setup_box, placeholder_text="SMTP 주소", **_e)
        e_smtp.pack(fill="x", pady=5)
        e_port = ctk.CTkEntry(setup_box, placeholder_text="포트 (465)", **_e)
        e_port.pack(fill="x", pady=5)

        # 분리형 인증에서만 노출되는 '보내는 사람 주소 (From)' 영역
        from_frame = ctk.CTkFrame(setup_box, fg_color="transparent")
        from_label = ctk.CTkLabel(
            from_frame,
            text="보내는 사람 주소 (From) — 수신자에게 보일 실제 주소",
            font=("맑은 고딕", 10),
            text_color="#f1c40f",
        )
        from_label.pack(anchor="w", pady=(0, 0))
        e_from = ctk.CTkEntry(from_frame, placeholder_text="예: noreply@yourdomain.com", **_e)
        e_from.pack(fill="x", pady=5)

        def _on_auth_type_change():
            if auth_type_var.get() == "separated":
                id_label.configure(text="SMTP 로그인 아이디 (AKIA...)")
                e_id.configure(placeholder_text="SMTP 로그인 아이디 (AKIA...)")
                try:
                    from_frame.pack(fill="x", pady=(2, 4), after=e_port)
                except Exception:
                    from_frame.pack(fill="x", pady=(2, 4))
            else:
                id_label.configure(text="아이디")
                e_id.configure(placeholder_text="아이디")
                from_frame.pack_forget()
        ctk.CTkLabel(
            setup_box,
            text="발송자 정보는 로그인 사용자 기준으로 공통 사용됩니다.",
            font=("맑은 고딕", 10),
            text_color="#95a5a6",
        ).pack(anchor="w", pady=(6, 2))
        ctk.CTkButton(
            setup_box,
            text="👤 내 프로필 열기",
            width=160,
            height=30,
            fg_color="#8e44ad",
            command=self._open_user_profile_popup,
            font=("맑은 고딕", 10),
        ).pack(anchor="w", pady=(0, 6))

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                d = json.load(f).get(task_key)
                if d:
                    if d.get("id"):
                        e_id.insert(0, d["id"])
                    if d.get("pw"):
                        e_pw.insert(0, d["pw"])
                    if d.get("smtp"):
                        e_smtp.insert(0, d["smtp"])
                    if d.get("port"):
                        e_port.insert(0, str(d["port"]))
                    # Phase 9: 하위호환 — auth_type 없으면 standard로 인식
                    _auth = (d.get("auth_type") or "standard").strip().lower()
                    if _auth not in ("standard", "separated"):
                        _auth = "standard"
                    auth_type_var.set(_auth)
                    if d.get("sender_email"):
                        e_from.insert(0, str(d["sender_email"]))
        except Exception:
            pass

        _on_auth_type_change()

        self._smtp_account_entries[task_key] = {
            "e_id": e_id,
            "e_pw": e_pw,
            "e_smtp": e_smtp,
            "e_port": e_port,
            "e_from": e_from,
            "auth_type_var": auth_type_var,
        }

        def verify():
            task_key = self._live_task_key()
            provider, idx = self._split_task_key(task_key)
            uid, upw, usmtp, uport = e_id.get().strip(), e_pw.get().strip(), e_smtp.get().strip(), e_port.get().strip()
            auth_type = auth_type_var.get().strip().lower()
            if auth_type not in ("standard", "separated"):
                auth_type = "standard"
            sender_email = e_from.get().strip()

            # Task 9-4: 분리형 인증인데 보내는 사람 주소가 비어 있으면 저장 차단
            if auth_type == "separated" and not sender_email:
                messagebox.showwarning(
                    "입력 확인",
                    "분리형 인증에서는 '보내는 사람 주소 (From)'를 반드시 입력해야 합니다.",
                    parent=self,
                )
                return
            if auth_type == "separated" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", sender_email):
                messagebox.showwarning(
                    "입력 확인",
                    "보내는 사람 주소 (From) 형식이 올바르지 않습니다.\n예: noreply@yourdomain.com",
                    parent=self,
                )
                return

            def check():
                try:
                    server = smtplib.SMTP_SSL(usmtp, int(uport), timeout=10)
                    server.login(uid, upw)
                    server.quit()
                    data = self._read_full_config()
                    prev = data.get(task_key) if isinstance(data.get(task_key), dict) else {}
                    entry = {
                        "id": uid,
                        "pw": upw,
                        "smtp": usmtp,
                        "port": uport,
                        "auth_type": auth_type,
                    }
                    if auth_type == "separated":
                        entry["sender_email"] = sender_email
                    if isinstance(prev, dict):
                        dn = (prev.get("display_name") or "").strip()
                        if dn:
                            entry["display_name"] = dn
                    data[task_key] = entry
                    self._atomic_write_config(data)
                    self._unconfigured_visible = [key for key in self._unconfigured_visible if key != task_key]
                    self.write_log(provider, idx, "✅ 계정 연동 성공")
                    self.after(0, self._rebuild_sidebar_buttons)
                except Exception as e:
                    self.write_log(provider, idx, f"❌ 실패: {e}")

            threading.Thread(target=check, daemon=True).start()

        ctk.CTkButton(
            setup_box,
            text="서버 연결 테스트 및 저장",
            fg_color="#28a745",
            command=verify,
            height=38,
            font=self._font_small,
        ).pack(fill="x", pady=12)

        list_f = ctk.CTkFrame(t2, fg_color="transparent")
        list_f.pack(fill="both", expand=True, padx=8, pady=8)
        tree_wrap = ctk.CTkFrame(list_f, fg_color="transparent")
        tree_wrap.pack(fill="both", expand=True, pady=(0, 6))
        tree = ttk.Treeview(
            tree_wrap, columns=("no", "comp", "email"), show="headings", height=8,
            selectmode="extended"
        )
        tree.column("no", width=48, anchor="center", stretch=False, minwidth=36)
        tree.column("comp", width=200, anchor="w", stretch=True, minwidth=60)
        tree.column("email", width=240, anchor="w", stretch=True, minwidth=80)
        tree.heading("no", text="No"); tree.heading("comp", text="업체명"); tree.heading("email", text="이메일")
        self.tree_views[task_key] = tree
        tree.pack(fill="both", expand=True)
        self._bind_recipients_tree_autosize(tree_wrap, tree)

        count_lbl = ctk.CTkLabel(list_f, text="등록된 수신처 없음", font=self._font_small, text_color="#bdc3c7")
        count_lbl.pack(anchor="w", pady=(0, 2))
        import_lbl = ctk.CTkLabel(list_f, text="", font=("맑은 고딕", 10), text_color="#95a5a6", anchor="w", justify="left")
        import_lbl.pack(anchor="w", pady=(0, 4))
        page_row = ctk.CTkFrame(list_f, fg_color="transparent")
        page_row.pack(fill="x", pady=(0, 4))
        page_prev = ctk.CTkButton(page_row, text="이전", width=70, command=self._recipient_prev_page)
        page_prev.pack(side="left", padx=(0, 4))
        page_label = ctk.CTkLabel(page_row, text="1 / 1", font=self._font_small)
        page_label.pack(side="left", padx=4)
        page_next = ctk.CTkButton(page_row, text="다음", width=70, command=self._recipient_next_page)
        page_next.pack(side="left", padx=4)

        def refresh_row_numbers():
            for i, iid in enumerate(tree.get_children(), 1):
                v = tree.item(iid)["values"]
                tree.item(iid, values=(i, v[1], v[2]))

        def update_count_label():
            n = len(tree.get_children())
            if n == 0:
                count_lbl.configure(text="등록된 수신처 없음")
            else:
                count_lbl.configure(text=f"총 {n}건 등록 (1 ~ {n}행)")

        def load_excel():
            self._on_load_excel_clicked(self._live_task_key(), tree, self._render_recipient_page)

        def clear_excel():
            task_key = self._live_task_key()
            self.save_recipients_rows(task_key, [], [])
            self._import_summaries.pop(task_key, None)
            self._recipient_page = 0
            self._render_recipient_page()

        def delete_selected():
            task_key = self._live_task_key()
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("안내", "삭제할 행을 선택해 주세요.", parent=t2)
                return
            emails = []
            for iid in sel:
                values = tree.item(iid).get("values") or []
                if len(values) > 2:
                    emails.append(values[2])
            if self.campaign_store:
                self.campaign_store.delete_account_recipients(self.login_user_id, task_key, emails)
            self._render_recipient_page()

        btn_row = ctk.CTkFrame(list_f, fg_color="transparent")
        btn_row.pack(fill="x", pady=4)
        btn_inner = ctk.CTkFrame(btn_row, fg_color="transparent")
        btn_inner.pack(fill="x")
        ctk.CTkButton(btn_inner, text="📁 엑셀 추가하기", command=load_excel, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkButton(btn_inner, text="🧹 전체 초기화", fg_color="#7f8c8d", command=clear_excel, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkButton(btn_inner, text="🗑 선택 행 삭제", fg_color="#c0392b", command=delete_selected, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkLabel(
            btn_row,
            text="(Ctrl·Shift+클릭으로 여러 행 선택)",
            font=("맑은 고딕", 10),
            text_color="#7f8c8d",
            anchor="w",
        ).pack(fill="x", padx=4, pady=(6, 0))

        export_btn_row = ctk.CTkFrame(list_f, fg_color="transparent")
        export_btn_row.pack(fill="x", pady=4)
        ctk.CTkButton(
            export_btn_row,
            text="📊 발송 결과 엑셀로 저장",
            fg_color="#9b59b6",
            command=lambda: self._export_to_excel(self._live_task_key()),
            font=self._font_small,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            export_btn_row,
            text="🚫 수신 거부 목록 관리",
            fg_color="#e74c3c",
            command=self._open_blacklist_manager,
            font=self._font_small,
        ).pack(side="left", padx=4)

        # 메시지 탭 전체 스크롤: 첨부/CID·배너 등으로 하단 버튼이 밀리지 않도록
        t3_scroll = ctk.CTkScrollableFrame(t3, fg_color="transparent")
        t3_scroll.pack(fill="both", expand=True, padx=0, pady=0)
        send_f = ctk.CTkFrame(t3_scroll, fg_color="transparent")
        send_f.pack(fill="x", expand=True, padx=8, pady=6)
        cur_d = {"files": [], "imgs": {}}

        # Phase 7 Task 7-2: 첨부/CID 요약 + 개별 삭제(높이는 3~4줄 분량으로 제한, 나머지는 스크롤)
        _attach_list_h = 56
        f_lbl = ctk.CTkLabel(send_f, text="📎 첨부: 없음", text_color="#95a5a6", font=self._font_small)
        f_lbl.pack(anchor="w", pady=(0, 2))
        files_list_inner = ctk.CTkScrollableFrame(send_f, height=_attach_list_h, fg_color="transparent")
        files_list_inner.pack(fill="x", pady=(0, 4))
        i_lbl = ctk.CTkLabel(send_f, text="🖼️ CID: 없음", text_color="#95a5a6", font=self._font_small)
        i_lbl.pack(anchor="w", pady=(0, 2))
        cid_list_inner = ctk.CTkScrollableFrame(send_f, height=_attach_list_h, fg_color="transparent")
        cid_list_inner.pack(fill="x", pady=(0, 6))

        def refresh_attach_ui():
            for w in files_list_inner.winfo_children():
                w.destroy()
            for w in cid_list_inner.winfo_children():
                w.destroy()
            files = list(cur_d.get("files") or [])
            imgs = cur_d.get("imgs") if isinstance(cur_d.get("imgs"), dict) else {}
            cur_d["files"] = files
            cur_d["imgs"] = imgs
            nf, ni = len(files), len(imgs)
            f_lbl.configure(text=f"📎 첨부: {nf}개" if nf else "📎 첨부: 없음")
            i_lbl.configure(text=f"🖼️ CID: {ni}개" if ni else "🖼️ CID: 없음")
            for i, path in enumerate(files):
                row = ctk.CTkFrame(files_list_inner, fg_color="#2b2b2b", corner_radius=4)
                row.pack(fill="x", pady=2, padx=2)
                disp = os.path.basename(str(path)) or str(path)
                ctk.CTkLabel(row, text=disp, anchor="w", font=self._font_small).pack(
                    side="left", fill="x", expand=True, padx=(8, 4), pady=4
                )

                def _remove_file_at(idx=i):
                    fl = list(cur_d.get("files") or [])
                    if 0 <= idx < len(fl):
                        fl.pop(idx)
                        cur_d["files"] = fl
                    refresh_attach_ui()

                ctk.CTkButton(
                    row,
                    text="✕",
                    width=32,
                    height=28,
                    fg_color="#c0392b",
                    hover_color="#a93226",
                    font=self._font_small,
                    command=_remove_file_at,
                ).pack(side="right", padx=4, pady=4)
            for cid_name, cpath in list(imgs.items()):
                row = ctk.CTkFrame(cid_list_inner, fg_color="#2b2b2b", corner_radius=4)
                row.pack(fill="x", pady=2, padx=2)
                c_disp = str(cid_name)
                p_disp = os.path.basename(str(cpath)) or str(cpath)
                ctk.CTkLabel(
                    row,
                    text=f"{c_disp}  →  {p_disp}",
                    anchor="w",
                    font=self._font_small,
                ).pack(side="left", fill="x", expand=True, padx=(8, 4), pady=4)

                def _remove_cid(cn=c_disp):
                    im = cur_d.get("imgs")
                    if isinstance(im, dict) and cn in im:
                        im.pop(cn, None)
                    refresh_attach_ui()

                ctk.CTkButton(
                    row,
                    text="✕",
                    width=32,
                    height=28,
                    fg_color="#c0392b",
                    hover_color="#a93226",
                    font=self._font_small,
                    command=_remove_cid,
                ).pack(side="right", padx=4, pady=4)

        toolbar = ctk.CTkFrame(send_f, fg_color="transparent")
        toolbar.pack(fill="x", pady=4)
        for c in range(8):
            toolbar.grid_columnconfigure(c, weight=0)
        toolbar.grid_columnconfigure(4, weight=1)
        ctk.CTkButton(
            toolbar,
            text="📂 템플릿",
            width=88,
            height=32,
            font=self._font_small,
            command=lambda: self.open_tpl_library(
                title_e, body_t, sender_e, cur_d, f_lbl, i_lbl, self._live_task_key(), refresh_attach_ui
            ),
        ).grid(row=0, column=0, padx=2, pady=2, sticky="w")
        ctk.CTkButton(
            toolbar,
            text="✍️ 에디터",
            width=88,
            height=32,
            fg_color="#2980b9",
            font=self._font_small,
            command=lambda: self._open_editor_for_body(body_t),
        ).grid(row=0, column=1, padx=2, pady=2, sticky="w")
        ctk.CTkButton(
            toolbar,
            text="🔍 미리보기",
            width=88,
            height=32,
            fg_color="#7f8c8d",
            font=self._font_small,
            command=lambda: self._open_message_preview(
                self._live_task_key(),
                title_e.get(),
                body_t.get("1.0", "end-1c"),
                tree,
            ),
        ).grid(row=0, column=2, padx=2, pady=2, sticky="w")
        ctk.CTkButton(
            toolbar,
            text="💾 저장",
            fg_color="#28a745",
            width=78,
            height=32,
            font=self._font_small,
            command=lambda: self.save_tpl(title_e, body_t, sender_e, cur_d, self._live_task_key()),
        ).grid(row=0, column=3, padx=2, pady=2, sticky="w")
        ctk.CTkButton(
            toolbar,
            text="📎 파일",
            width=78,
            height=32,
            fg_color="#555",
            font=self._font_small,
            command=lambda: self.attach_file(cur_d, refresh_attach_ui),
        ).grid(row=0, column=6, padx=2, pady=2, sticky="e")
        ctk.CTkButton(
            toolbar,
            text="🖼️ CID",
            width=78,
            height=32,
            fg_color="#555",
            font=self._font_small,
            command=lambda: self.attach_cid(cur_d, refresh_attach_ui),
        ).grid(row=0, column=7, padx=2, pady=2, sticky="e")

        refresh_attach_ui()

        title_e = ctk.CTkEntry(send_f, placeholder_text="제목 {업체명}", height=38, font=self._font_body); title_e.pack(fill="x", pady=4)
        sender_e = ctk.CTkEntry(send_f, placeholder_text="보내는 사람 이름", height=38, border_color="#1F6AA5", font=self._font_body); sender_e.pack(fill="x", pady=4)
        ctk.CTkLabel(
            send_f,
            text="내 정보 변수: {{내이름}} {{내직책}} {{내전화번호}} {{내이메일}}",
            font=("맑은 고딕", 10),
            text_color="#95a5a6",
        ).pack(anchor="w", pady=(0, 4))
        tag_btn_row = ctk.CTkFrame(send_f, fg_color="transparent")
        tag_btn_row.pack(fill="x", pady=(0, 4))
        tag_btn_row.grid_columnconfigure(1, weight=1)
        tag_btn_row.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(tag_btn_row, text="빠른 삽입:", font=("맑은 고딕", 10), text_color="#95a5a6").grid(
            row=0, column=0, rowspan=2, padx=(0, 8), pady=2, sticky="nw"
        )

        def _insert_user_tag(token):
            try:
                body_t.insert("insert", token)
                body_t.focus_set()
            except Exception:
                pass

        for i, _token in enumerate(("{{내이름}}", "{{내직책}}", "{{내전화번호}}", "{{내이메일}}")):
            ctk.CTkButton(
                tag_btn_row,
                text=_token,
                height=28,
                fg_color="#3b3b3b",
                font=("맑은 고딕", 10),
                command=lambda t=_token: _insert_user_tag(t),
            ).grid(row=i // 2, column=1 + (i % 2), padx=2, pady=2, sticky="ew")
        body_t = ctk.CTkTextbox(send_f, height=110, font=self._font_body)
        body_t.pack(fill="both", expand=True, pady=4)

        interval_f = ctk.CTkFrame(send_f, fg_color="transparent")
        interval_f.pack(fill="x", pady=4)
        ctk.CTkLabel(interval_f, text="전송 간격", width=80, font=self._font_small).pack(side="left", padx=(0, 8))
        interval_cb = ctk.CTkComboBox(
            interval_f, values=["1분", "2분", "3분", "5분", "10분", "랜덤(1~10분)"],
            width=150, height=32, state="readonly", font=self._font_small
        )
        interval_cb.set("5분")
        interval_cb.pack(side="left")

        prevent_dup_var = ctk.BooleanVar(value=True)
        dup_hint_f = ctk.CTkFrame(send_f, fg_color="transparent")
        dup_hint_f.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            dup_hint_f,
            text="중복 차단: 같은 로그인 계정+같은 이메일+같은 본문(HTML 해시)이면 스킵합니다. (템플릿명이 같아도 본문을 바꾸면 발송 가능)",
            font=("맑은 고딕", 11),
            anchor="w",
            justify="left",
        ).pack(fill="x", anchor="w")

        public_filter_var = ctk.BooleanVar(value=False)
        filter_banner = ctk.CTkFrame(send_f, fg_color="#4a3a10", corner_radius=8)
        filter_banner.pack(fill="x", pady=(10, 6))
        ctk.CTkCheckBox(
            filter_banner,
            text="공공기관/단체 필터 적용  (go.kr, or.kr, 협회·학회·조합·중앙회·공사·공단·재단 등)",
            variable=public_filter_var,
            font=("맑은 고딕", 12, "bold"),
            text_color="#ffeaa7",
            fg_color="#d4a017",
            hover_color="#c9a227",
            checkbox_width=22,
            checkbox_height=22,
        ).pack(anchor="w", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            filter_banner,
            text="켜면 해당 수신처는 발송하지 않으며, 로컬 발송 기록(DB)에도 남기지 않습니다.",
            font=("맑은 고딕", 10),
            text_color="#dfe6e9",
        ).pack(anchor="w", padx=12, pady=(0, 10))

        hours_banner = ctk.CTkFrame(send_f, fg_color="#14332a", corner_radius=8)
        hours_banner.pack(fill="x", pady=(4, 6))
        ctk.CTkLabel(
            hours_banner,
            text=POLICY_TEXT,
            font=("맑은 고딕", 12, "bold"),
            text_color="#b8f5d1",
        ).pack(anchor="w", padx=12, pady=(8, 2))
        camp_status = ctk.CTkLabel(
            hours_banner,
            text="현재 작업 상태: 대기",
            font=self._font_small,
            text_color="#ecf0f1",
            anchor="w",
            justify="left",
        )
        camp_status.pack(fill="x", padx=12, pady=(0, 2))
        camp_account = ctk.CTkLabel(
            hours_banner,
            text=f"계정: {self._sidebar_label_text(task_key, self.task_key_to_index.get(task_key, 0) or 0)}",
            font=self._font_small,
            text_color="#dfe6e9",
            anchor="w",
            justify="left",
        )
        camp_account.pack(fill="x", padx=12, pady=(0, 2))
        camp_resume = ctk.CTkLabel(
            hours_banner,
            text="다음 자동 재개 예정: -",
            font=self._font_small,
            text_color="#dfe6e9",
            anchor="w",
            justify="left",
        )
        camp_resume.pack(fill="x", padx=12, pady=(0, 2))
        camp_counts = ctk.CTkLabel(
            hours_banner,
            text="전체 0 · 성공 0 · 건너뜀 0 · 실패 0 · 남은 0",
            font=self._font_small,
            text_color="#dfe6e9",
            anchor="w",
            justify="left",
        )
        camp_counts.pack(fill="x", padx=12, pady=(0, 2))
        camp_autostart = ctk.CTkLabel(
            hours_banner,
            text="PC 재부팅 후 자동복구: 비활성",
            font=self._font_small,
            text_color="#95a5a6",
            anchor="w",
            justify="left",
        )
        camp_autostart.pack(fill="x", padx=12, pady=(0, 4))
        camp_fix = ctk.CTkButton(
            hours_banner,
            text="확인 필요 해결",
            width=160,
            command=lambda: self._open_attention_for_key(self._live_task_key()),
        )
        camp_fix.pack(anchor="w", padx=12, pady=(0, 8))
        camp_fix.pack_forget()
        self.campaign_ui[task_key] = {
            "status": camp_status,
            "account": camp_account,
            "resume": camp_resume,
            "counts": camp_counts,
            "autostart": camp_autostart,
            "fix": camp_fix,
        }

        def start():
            task_key = self._live_task_key()
            provider, idx = self._split_task_key(task_key)
            if self._start_in_flight.get(task_key) or self._engine_locks.get(task_key) and self._engine_locks[task_key].locked():
                return
            if self.campaign_store:
                blocking = self.campaign_store.find_blocking_job(self.login_user_id, task_key)
                if blocking:
                    job_st = blocking.get("status")
                    if job_st == JOB_NEEDS_ATTENTION:
                        self._prompt_needs_attention(blocking)
                        return
                    if blocking.get("status") in RESUME_JOB_STATUSES:
                        if self._engine_locks.get(task_key) and self._engine_locks[task_key].locked():
                            return
            interval = interval_cb.get()
            prevent_dup = prevent_dup_var.get()
            apply_public_filter = public_filter_var.get()
            profile = self.get_login_user_profile()
            merged_text = f"{title_e.get()}\n{body_t.get('1.0', 'end-1c')}"
            used_tags = [t for t in ("{{내이름}}", "{{내직책}}", "{{내전화번호}}", "{{내이메일}}") if t in merged_text]
            missing = []
            if "{{내이름}}" in used_tags and not (profile.get("user_name") or "").strip():
                missing.append("내이름")
            if "{{내직책}}" in used_tags and not (profile.get("user_rank") or "").strip():
                missing.append("내직책")
            if "{{내전화번호}}" in used_tags and not (profile.get("user_phone") or "").strip():
                missing.append("내전화번호")
            if "{{내이메일}}" in used_tags and not (profile.get("user_email") or "").strip():
                missing.append("내이메일")
            if missing:
                proceed = messagebox.askyesno(
                    "발송자 정보 확인",
                    "아래 발송자 정보가 비어 있어 치환 시 빈값으로 전송됩니다.\n"
                    f"- {', '.join(missing)}\n\n"
                    "계속 진행할까요?",
                    parent=t3,
                )
                if not proceed:
                    return
            sender_name = sender_e.get().strip()
            if not sender_name:
                sender_name = (profile.get("user_name") or "").strip() or (self.user_name or "").strip()

            # 1계정당 1템플릿 규칙: 템플릿 라이브러리명 우선, 없으면 제목 (Task 2-2: real_engine과 동일 키)
            template_name = _dedup_template_key(self.current_template_name.get(task_key), title_e.get())
            self.current_template_name[task_key] = template_name

            self._start_in_flight[task_key] = True
            self.stop_flags[task_key] = False
            self._set_send_buttons(task_key, "running")
            threading.Thread(
                target=self.real_engine,
                args=(
                    provider,
                    idx,
                    title_e.get(),
                    body_t.get("1.0", "end-1c"),
                    sender_name,
                    {"files": list(cur_d.get("files") or []), "imgs": dict(cur_d.get("imgs") or {})},
                    interval,
                    prevent_dup,
                    apply_public_filter,
                    tree,
                    send_b,
                    stop_b,
                    template_name,
                ),
                daemon=True,
            ).start()

        # 시작/중지 버튼은 아래 위젯에 밀리지 않도록 interval 아래에 배치
        btn_f = ctk.CTkFrame(send_f, fg_color="transparent")
        btn_f.pack(fill="x", pady=8)
        send_b = ctk.CTkButton(btn_f, text="🚀 자동발송 시작", height=42, font=self._font_small, command=start)
        send_b.pack(side="left", fill="x", expand=True, padx=4)
        test_b = ctk.CTkButton(
            btn_f,
            text="🧪 테스트 발송",
            height=42,
            fg_color="#3498db",
            font=self._font_small,
            command=lambda: self._start_test_for_current(title_e, body_t, sender_e, cur_d, send_b, test_b),
        )
        test_b.pack(side="left", fill="x", expand=True, padx=4)
        stop_b = ctk.CTkButton(btn_f, text="🛑 중지", height=42, state="disabled", font=self._font_small, command=lambda: self.set_stop(self._live_task_key()))
        stop_b.pack(side="right", fill="x", expand=True, padx=4)
        cancel_b = ctk.CTkButton(
            btn_f,
            text="작업 취소",
            height=42,
            width=90,
            state="disabled",
            fg_color="#7f8c8d",
            font=self._font_small,
            command=lambda: self.set_cancel(self._live_task_key()),
        )
        cancel_b.pack(side="right", padx=4)
        self.campaign_buttons[task_key] = {"start": send_b, "stop": stop_b, "cancel": cancel_b, "test": test_b}

        log_t = ctk.CTkTextbox(send_f, height=72, font=("Consolas", 11), fg_color="#1e1e1e", text_color="#00ff00")
        log_t.pack(fill="x", pady=4)
        log_t.configure(state="disabled"); self.log_consoles[task_key] = log_t

        progress_lbl = ctk.CTkLabel(send_f, text="마지막 성공 전송: 없음", font=self._font_small, text_color="#bdc3c7")
        progress_lbl.pack(anchor="w", pady=(2, 0))
        self.progress_labels[task_key] = progress_lbl
        self._shared_widgets = {
            "tree": tree,
            "count": count_lbl,
            "import": import_lbl,
            "page": page_label,
            "log": log_t,
            "buttons": self.campaign_buttons[task_key],
            "campaign_ui": self.campaign_ui[task_key],
            "progress": progress_lbl,
            "smtp": self._smtp_account_entries[task_key],
            "title": title_e,
            "body": body_t,
            "sender": sender_e,
            "data": cur_d,
            "interval": interval_cb,
            "prevent_dup": prevent_dup_var,
            "public_filter": public_filter_var,
            "refresh_attach": refresh_attach_ui,
        }

        # (start/버튼은 위에서 이미 배치됨)

    def _apply_dynamic_variables(self, text, row_data):
        """엑셀 데이터를 이용해 동적 변수 치환 ({변수명} → 값)"""
        result = text
        for key, value in row_data.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value or ""))
        return result

    def replace_user_variables(self, text, task_key=None):
        """로그인 사용자 프로필 기반 발송자 정보 태그({{내이름}} 등) 치환."""
        result = str(text or "")
        profile = self.get_login_user_profile()
        mapping = {
            "{{내이름}}": profile.get("user_name", ""),
            "{{내직책}}": profile.get("user_rank", ""),
            "{{내전화번호}}": profile.get("user_phone", ""),
            "{{내이메일}}": profile.get("user_email", ""),
        }
        for token, value in mapping.items():
            result = result.replace(token, str(value or ""))
        return result

    def _render_message_with_variables(self, task_key, title, body, row_data=None):
        """엑셀 변수 + 발송자 변수까지 포함한 최종 렌더링 결과."""
        row = row_data if isinstance(row_data, dict) else {}
        final_title = self._apply_dynamic_variables(str(title or ""), row)
        final_body = self._apply_dynamic_variables(str(body or ""), row)
        final_title = self.replace_user_variables(final_title, task_key)
        final_body = self.replace_user_variables(final_body, task_key)
        return final_title, final_body

    def _open_message_preview(self, task_key, title, body, tree_widget=None):
        """메시지 발송 전 최종 치환 결과를 확인하는 미리보기 창."""
        rows = []
        if self.campaign_store:
            rows = self.campaign_store.list_account_recipients_page(self.login_user_id, task_key, 0, 1)
        else:
            rows = self.load_recipients_state(task_key, include_rows=True).get("rows", [])
        sample_row = {}
        if isinstance(rows, list) and rows:
            # 수신처 탭에서 선택한 행이 있으면 그 행을 샘플로 사용
            sel = []
            try:
                if tree_widget is not None:
                    sel = list(tree_widget.selection())
            except Exception:
                sel = []
            if sel and tree_widget is not None:
                try:
                    v = tree_widget.item(sel[0]).get("values", [])
                    no = int(v[0]) if v else 1
                    if 1 <= no <= len(rows) and isinstance(rows[no - 1], dict):
                        sample_row = rows[no - 1]
                except Exception:
                    pass
            if not sample_row:
                for r in rows:
                    if isinstance(r, dict):
                        sample_row = r
                        break
        if not sample_row:
            sample_row = {"업체명": "샘플업체", "이메일": "sample@example.com"}
        final_title, final_body = self._render_message_with_variables(task_key, title, body, sample_row)
        unresolved_tokens = sorted(set(re.findall(r"\{\{[^{}]+\}\}", f"{final_title}\n{final_body}")))

        pop = self.dialogs.open_toplevel(
            "message_preview",
            title="메시지 미리보기",
            geometry="760x620",
            minsize=(440, 400),
            modal=False,
        )
        if pop is None:
            return
        if getattr(pop, "_ui_dialog_built", False):
            return
        pop._ui_dialog_built = True
        ctk.CTkLabel(pop, text="최종 치환 미리보기", font=("맑은 고딕", 15, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            pop,
            text=f"샘플 수신처 기준: {sample_row.get('업체명', '')} <{sample_row.get('이메일', '')}>",
            font=("맑은 고딕", 11),
            text_color="#95a5a6",
        ).pack(anchor="w", padx=12, pady=(0, 8))
        ctk.CTkLabel(pop, text="제목", font=("맑은 고딕", 12, "bold")).pack(anchor="w", padx=12)
        title_box = ctk.CTkEntry(pop, height=34, font=self._font_body)
        title_box.pack(fill="x", padx=12, pady=(4, 8))
        title_box.insert(0, final_title)
        ctk.CTkLabel(pop, text="본문 (HTML 원문)", font=("맑은 고딕", 12, "bold")).pack(anchor="w", padx=12)
        body_box = ctk.CTkTextbox(pop, font=("Consolas", 11))
        body_box.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        body_box.insert("1.0", final_body)
        if unresolved_tokens:
            ctk.CTkLabel(
                pop,
                text=f"주의: 미치환 태그 발견 → {' '.join(unresolved_tokens)}",
                font=("맑은 고딕", 10),
                text_color="#f39c12",
            ).pack(anchor="w", padx=12, pady=(0, 8))

        def _open_html_render():
            html = final_body if "<" in final_body and ">" in final_body else f"<pre>{final_body}</pre>"
            wrapper = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>MAIL MONSTER Preview</title></head>"
                f"<body>{html}</body></html>"
            )
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".html", delete=False) as tf:
                    tf.write(wrapper)
                    temp_path = tf.name
                webbrowser.open(f"file:///{temp_path.replace(os.sep, '/')}")
            except Exception as e:
                messagebox.showerror("미리보기 오류", f"브라우저 미리보기를 열 수 없습니다.\n{e}", parent=pop)

        btn_row = ctk.CTkFrame(pop, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(btn_row, text="🌐 브라우저 렌더 보기", width=180, command=_open_html_render).pack(side="left")
        ctk.CTkButton(
            btn_row,
            text="닫기",
            width=100,
            command=lambda: self._close_managed_window(pop, "message_preview"),
        ).pack(side="right")

    def _translate_smtp_error(self, err_msg):
        """Task 3-2: SMTP 에러 코드(550, 553, 535 등)를 한국어로 보충 설명."""
        if not err_msg or not isinstance(err_msg, str):
            return err_msg or ""
        s = err_msg.strip()
        hints = []
        if "550" in s or "5.7.1" in s:
            hints.append("수신 거부(550): 메일함 없음/거부 정책")
        if "553" in s or "5.5.4" in s:
            hints.append("주소 오류(553): 수신 주소 형식 오류")
        if "535" in s or "5.7.8" in s or "Authentication" in s:
            hints.append("인증 실패(535): 앱 비밀번호·보안 설정 확인")
        if "554" in s:
            hints.append("전송 실패(554): 스팸/정책 차단 가능")
        if "421" in s or "4.7.0" in s:
            hints.append("연결 제한(421): 동시 접속 제한·잠시 후 재시도")
        if "552" in s or "5.2.2" in s:
            hints.append("메일함 초과(552): 수신자 메일함 용량 초과")
        if hints:
            return f"{s} → {' | '.join(hints)}"
        return s

    def _split_task_key(self, key):
        s = str(key or "")
        if "_" not in s:
            return s, 1
        p, i = s.rsplit("_", 1)
        try:
            return p, int(i)
        except ValueError:
            return s, 1

    def _campaign_event_is_current(self, key, job_id=None, generation=None):
        if job_id is None and generation is None:
            return True
        return event_matches_current(
            event_task_key=key,
            event_job_id=job_id,
            event_generation=int(generation or 0),
            current_task_key=key,
            current_job_id=self.campaign_job_ids.get(key) or "",
            current_generation=int(self.campaign_generations.get(key) or 0),
        )

    def _set_send_buttons(self, key, status, *, job_id=None, generation=None):
        if job_id is not None and not self._campaign_event_is_current(key, job_id, generation):
            return
        self._button_modes[key] = status
        if key != self._live_task_key():
            return
        btns = (self._shared_widgets or {}).get("buttons") or self.campaign_buttons.get(key) or {}
        start_b, stop_b, cancel_b = btns.get("start"), btns.get("stop"), btns.get("cancel")
        aliases = {
            "running": JOB_RUNNING,
            "paused": JOB_SCHEDULED_PAUSE,
            "idle": "",
        }
        status = aliases.get(status, status)
        spec = button_state_for_status(status)

        def apply():
            if not self._campaign_event_is_current(key, job_id, generation):
                return
            try:
                if start_b:
                    start_b.configure(state=spec["start_state"], text=spec["start_text"])
                if stop_b:
                    stop_b.configure(
                        state=spec["stop_state"],
                        fg_color="#dc3545" if spec["stop_state"] == "normal" else "#555",
                    )
                if cancel_b:
                    cancel_b.configure(state=spec["cancel_state"])
                fix_b = (self.campaign_ui.get(key) or {}).get("fix")
                if fix_b:
                    if spec["fix_visible"]:
                        fix_b.pack(anchor="w", padx=12, pady=(0, 8))
                    else:
                        fix_b.pack_forget()
            except Exception:
                pass

        schedule_on_ui(self, apply)

    def _campaign_recipient_rows(self, task_key):
        """선택 SMTP 계정에 귀속된 수신처만 정규화 이메일 기준으로 반환한다."""
        seen = set()
        out = []
        rows = []
        if self.campaign_store:
            rows = self.campaign_store.list_account_recipients(self.login_user_id, task_key)
        else:
            rows = self.load_recipients_state(task_key, include_rows=True).get("rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            email = str(row.get("이메일") or row.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            out.append(row)
        return out

    def _task_keys_for_job(self, job_id):
        keys = []
        try:
            for w in self.campaign_store.list_workers(job_id):
                k = w.get("task_key") or ""
                if k and k not in keys:
                    keys.append(k)
        except Exception:
            pass
        return keys

    def _bind_campaign_job(self, key, job_or_id, worker_id=None):
        job = (
            self.campaign_store.get_job(job_or_id)
            if isinstance(job_or_id, str)
            else (job_or_id or {})
        ) or {}
        job_id = job.get("job_id") or (job_or_id if isinstance(job_or_id, str) else "")
        if not job_id:
            return {}
        self.campaign_job_ids[key] = job_id
        self.campaign_generations[key] = int(job.get("generation") or 0)
        wid = worker_id or job.get("worker_id") or self.campaign_store.primary_worker_id(job_id, key)
        if wid:
            self.campaign_worker_ids[key] = wid
        return job

    def _refresh_campaign_ui(self, key, *, job_id=None, generation=None):
        ui = self.campaign_ui.get(key)
        if not ui or not self.campaign_store:
            return
        if key != self._live_task_key():
            return
        if job_id is not None and not self._campaign_event_is_current(key, job_id, generation):
            return
        job_id = self.campaign_job_ids.get(key)
        if not job_id:
            latest = self.campaign_store.find_latest_job(self.login_user_id, key)
            if latest:
                job_id = latest.get("job_id")
                self.campaign_job_ids[key] = job_id
                self.campaign_generations[key] = int(latest.get("generation") or 0)
                wid = self.campaign_store.primary_worker_id(job_id, key)
                if wid:
                    self.campaign_worker_ids[key] = wid
        worker_id = self.campaign_worker_ids.get(key)
        stats = self.campaign_store.stats_dict(job_id, worker_id=worker_id, task_key=key) if job_id else {}
        if not job_id:
            total = len(self.load_recipients_state(key).get("rows") or [])
            stats = {"total": total, "remaining": total}
        status = stats.get("status") or ""
        ko = CAMPAIGN_STATUS_KO.get(status, status or "대기")
        nxt = stats.get("next_resume_at") or ""
        acct = self._sidebar_label_text(key, self.task_key_to_index.get(key, 0) or 0)

        def apply():
            if job_id and not self._campaign_event_is_current(
                key,
                job_id,
                self.campaign_generations.get(key),
            ):
                return
            try:
                if ui.get("account"):
                    ui["account"].configure(text=f"계정: {acct}")
                ui["status"].configure(text=f"현재 작업 상태: {ko}")
                if status == JOB_SCHEDULED_PAUSE:
                    try:
                        dt = datetime.fromisoformat(nxt) if nxt else self.business_hours.next_send_window_start()
                        ui["resume"].configure(text=self.business_hours.format_resume_text(dt))
                    except Exception:
                        ui["resume"].configure(text=f"다음 발송 또는 재개: {nxt or '-'}")
                elif status == JOB_RUNNING:
                    ui["resume"].configure(text="다음 발송 또는 재개: 현재 발송 가능 시간")
                else:
                    ui["resume"].configure(text="다음 발송 또는 재개: -")
                sent = int(stats.get("success") or 0)
                remaining = int(stats.get("remaining") or 0)
                ui["counts"].configure(
                    text=(
                        f"발송 완료 {sent} · 남은 {remaining} · "
                        f"전체 {int(stats.get('total') or 0)} · 건너뜀 {int(stats.get('skipped') or 0)} · "
                        f"실패 {int(stats.get('failed') or 0)}"
                    )
                )
                auto = "활성" if is_autostart_enabled() else "비활성"
                ui["autostart"].configure(text=f"PC 재부팅 후 자동복구: {auto}")
            except Exception:
                pass

        schedule_on_ui(self, apply)
        self._set_send_buttons(
            key,
            status,
            job_id=job_id if job_id else None,
            generation=self.campaign_generations.get(key) if job_id else None,
        )

    def _sync_autostart_registry(self):
        try:
            active = bool(self.campaign_store and self.campaign_store.any_active_on_pc())
            sync_autostart(active, base_dir=BASE_DIR)
        except Exception:
            pass
        for k in list(self.campaign_ui.keys()):
            self._refresh_campaign_ui(k)

    def _lookup_sent_for_campaign_item(self, item, job=None):
        mid = str((item or {}).get("message_id") or "").strip()
        if not mid:
            return False
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute("SELECT 1 FROM sent_log WHERE message_id=? LIMIT 1", (mid,)).fetchone()
            return row is not None
        except Exception:
            return False
        finally:
            con.close()

    def _open_attention_for_key(self, key):
        if not self.campaign_store:
            return
        job = None
        jid = self.campaign_job_ids.get(key)
        if jid:
            job = self.campaign_store.get_job(jid)
        if not job or job.get("status") != JOB_NEEDS_ATTENTION:
            blocking = self.campaign_store.find_blocking_job(self.login_user_id, key)
            if blocking and blocking.get("status") == JOB_NEEDS_ATTENTION:
                job = blocking
        if not job or job.get("status") != JOB_NEEDS_ATTENTION:
            messagebox.showinfo("안내", "확인할 작업이 없습니다.", parent=self)
            return
        self._prompt_needs_attention(job)

    def _attention_next_resume_iso(self) -> str:
        return self.business_hours.next_send_window_start().isoformat(timespec="seconds")

    def _release_attention_if_ready(self, job) -> str:
        if not job:
            return JOB_NEEDS_ATTENTION
        job_id = job["job_id"]
        key = job.get("task_key") or ""
        live = resolve_smtp_for_send(self.config_file, key, self.campaign_store.job_smtp_config(job))
        if not live:
            self.campaign_store.set_needs_attention(
                job_id, "SMTP 계정 자격증명을 찾을 수 없습니다. 계정 설정에서 비밀번호를 확인하세요."
            )
            return JOB_NEEDS_ATTENTION
        return maybe_release_attention(
            self.campaign_store,
            job_id,
            send_allowed=self.business_hours.is_send_allowed(),
            next_resume_at=self._attention_next_resume_iso(),
            now=self.business_hours.now(),
        )

    def _try_resume_after_attention(self, job) -> bool:
        st = self._release_attention_if_ready(job)
        return st in (JOB_RUNNING, JOB_SCHEDULED_PAUSE, JOB_COMPLETED)

    def _start_after_attention_release(self, job_id, key, status):
        self._sync_autostart_registry()
        self._refresh_campaign_ui(key)
        if status in (JOB_RUNNING, JOB_SCHEDULED_PAUSE):
            workers = []
            try:
                workers = self.campaign_store.list_resumable_workers(self.login_user_id)
            except Exception:
                workers = []
            started = False
            for w in workers:
                if w.get("job_id") != job_id:
                    continue
                wk = w.get("task_key") or key
                self._bind_campaign_job(wk, job_id, w.get("worker_id"))
                self._set_send_buttons(wk, status)
                btns = self.campaign_buttons.get(wk) or {}
                threading.Thread(
                    target=self._start_campaign_runner,
                    args=(job_id, wk, btns.get("start"), btns.get("stop")),
                    daemon=True,
                ).start()
                started = True
            if not started:
                self._set_send_buttons(key, status)
                btns = self.campaign_buttons.get(key) or {}
                threading.Thread(
                    target=self._start_campaign_runner,
                    args=(job_id, key, btns.get("start"), btns.get("stop")),
                    daemon=True,
                ).start()
        elif status == JOB_NEEDS_ATTENTION:
            self._set_send_buttons(key, JOB_NEEDS_ATTENTION)
        else:
            self._set_send_buttons(key, status)

    def _prompt_needs_attention(self, job):
        if not job:
            return
        job_id = job["job_id"]
        key = job.get("task_key") or ""
        attn_key = f"attention_{job_id}"
        win = self.dialogs.open_toplevel(
            attn_key,
            title="발송 확인 필요",
            geometry="760x620",
            modal=True,
        )
        if win is None:
            return
        if getattr(win, "_ui_dialog_built", False):
            return
        win._ui_dialog_built = True

        reason_lbl = ctk.CTkLabel(win, text="", font=self._font_small, justify="left", anchor="w", wraplength=720)
        reason_lbl.pack(fill="x", padx=12, pady=(12, 4))
        missing_box = ctk.CTkTextbox(win, height=90, font=self._font_small)
        missing_box.pack(fill="x", padx=12, pady=(0, 6))
        list_host = ctk.CTkScrollableFrame(win, height=260)
        list_host.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        hint = ctk.CTkLabel(
            win,
            text="needs_review 항목은 자동으로 다시 보내지 않습니다. 항목마다 처리하세요.",
            font=self._font_small,
            text_color="#f8c471",
            anchor="w",
            justify="left",
        )
        hint.pack(fill="x", padx=12, pady=(0, 6))
        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 12))

        def current_job():
            return self.campaign_store.get_job(job_id) or {}

        def refresh():
            live = current_job()
            reason = live.get("attention_reason") or "사용자 확인이 필요합니다."
            attach = self.campaign_store.job_snapshot_attachments(live)
            missing = missing_attachment_paths(attach)
            reason_lbl.configure(text=reason)
            missing_box.configure(state="normal")
            missing_box.delete("1.0", "end")
            if missing:
                missing_box.insert("1.0", "누락된 파일 경로:\n" + "\n".join(missing))
            else:
                files = list((attach.get("files") or []))
                imgs = dict((attach.get("imgs") or {}))
                lines = ["첨부 파일은 모두 존재합니다."]
                if files:
                    lines.append("첨부: " + ", ".join(str(p) for p in files[:8]))
                if imgs:
                    lines.append("CID: " + ", ".join(f"{k}={v}" for k, v in list(imgs.items())[:8]))
                missing_box.insert("1.0", "\n".join(lines))
            missing_box.configure(state="disabled")
            for child in list_host.winfo_children():
                child.destroy()
            items = list_review_items(self.campaign_store, job_id)
            if not items:
                ctk.CTkLabel(list_host, text="확인할 수신자가 없습니다.", anchor="w").pack(fill="x", pady=4)
            for it in items:
                row = ctk.CTkFrame(list_host, fg_color="#1b2631")
                row.pack(fill="x", pady=3)
                email = it.get("email") or ""
                company = it.get("company") or ""
                err = it.get("error_message") or ""
                ctk.CTkLabel(
                    row,
                    text=f"{company} <{email}>\n{err}",
                    anchor="w",
                    justify="left",
                    wraplength=420,
                ).pack(side="left", padx=8, pady=6, fill="x", expand=True)
                iid = int(it["id"])
                ctk.CTkButton(
                    row,
                    text="발송 완료로 처리",
                    width=130,
                    command=lambda n=iid: on_item(n, ACTION_MARK_SENT),
                ).pack(side="right", padx=4, pady=6)
                ctk.CTkButton(
                    row,
                    text="다시 발송",
                    width=90,
                    fg_color="#b9770e",
                    command=lambda n=iid: on_item(n, ACTION_RESEND),
                ).pack(side="right", padx=4, pady=6)
                ctk.CTkButton(
                    row,
                    text="건너뛰기",
                    width=80,
                    fg_color="#7f8c8d",
                    command=lambda n=iid: on_item(n, ACTION_SKIP),
                ).pack(side="right", padx=4, pady=6)

        def finish_if_released():
            live = current_job()
            st = self._release_attention_if_ready(live)
            if st == JOB_NEEDS_ATTENTION:
                refresh()
                return False
            try:
                self._close_managed_window(win, attn_key)
            except Exception:
                pass
            self._start_after_attention_release(job_id, key, st)
            return True

        def on_item(item_id, action):
            note = ""
            confirmed = False
            if action == ACTION_RESEND:
                if not messagebox.askyesno("중복 발송 경고", RESEND_WARNING + "\n\n이 수신자만 다시 대기열에 넣습니다.", parent=win):
                    return
                confirmed = True
            elif action == ACTION_SKIP:
                note = self.dialogs.askstring("건너뛰기", "사유를 입력하세요.", key="attention_skip", parent=win) or ""
                if not note.strip():
                    messagebox.showwarning("사유 필요", "건너뛰기에는 사유가 필요합니다.", parent=win)
                    return
            elif action == ACTION_MARK_SENT:
                if not messagebox.askyesno("발송 완료로 처리", "이미 발송된 것으로 기록합니다. 메일을 다시 보내지 않습니다.", parent=win):
                    return
            try:
                resolve_review_item(
                    self.campaign_store,
                    item_id,
                    action,
                    note=note,
                    now=self.business_hours.now(),
                    resend_confirmed=confirmed,
                )
            except ReviewActionError as e:
                messagebox.showwarning("처리 실패", str(e), parent=win)
                return
            finish_if_released()

        def on_rebind_files():
            files = self.dialogs.askopenfilenames(key="attention_rebind_files", title="첨부파일 다시 지정", parent=win)
            if not files:
                return
            _, missing = replace_job_attachments(self.campaign_store, job_id, files=list(files))
            if missing:
                messagebox.showwarning("파일 확인", "지정한 파일이 없거나 다른 첨부/CID가 아직 없습니다.\n" + "\n".join(missing[:12]), parent=win)
            finish_if_released()

        def on_rebind_cids():
            live = current_job()
            attach = self.campaign_store.job_snapshot_attachments(live)
            keys = list((attach.get("imgs") or {}).keys())
            files = self.dialogs.askopenfilenames(key="attention_rebind_cids", title="CID 이미지 다시 지정", parent=win)
            if not files:
                return
            if keys:
                imgs = {keys[i]: files[i] for i in range(min(len(keys), len(files)))}
                if len(files) > len(keys):
                    for extra in files[len(keys):]:
                        imgs[os.path.basename(extra)] = extra
            else:
                imgs = {os.path.basename(p): p for p in files}
            _, missing = replace_job_attachments(self.campaign_store, job_id, imgs=imgs)
            if missing:
                messagebox.showwarning("파일 확인", "아직 없는 파일이 있습니다.\n" + "\n".join(missing[:12]), parent=win)
            finish_if_released()

        def on_cancel_job():
            if not messagebox.askyesno("캠페인 취소", "이 발송 작업을 취소합니다. 남은 수신자는 보내지 않습니다.", parent=win):
                return
            cancel_campaign(self.campaign_store, job_id, now=self.business_hours.now())
            try:
                self._close_managed_window(win, attn_key)
            except Exception:
                pass
            self._start_after_attention_release(job_id, key, JOB_CANCELLED)

        ctk.CTkButton(btn_row, text="첨부파일 다시 지정", command=on_rebind_files).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="CID 이미지 다시 지정", command=on_rebind_cids).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_row,
            text="캠페인 취소",
            fg_color="#922b21",
            command=on_cancel_job,
        ).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="닫기", fg_color="#566573", command=lambda: self._close_managed_window(win, attn_key)).pack(side="right", padx=4)
        refresh()

    def _ensure_campaign_job(self, p, i, title, body, s_name, data, interval, prevent_dup, apply_public_filter, template_name):
        key = f"{p}_{i}"
        existing = self.campaign_store.find_blocking_job(self.login_user_id, key)
        if existing:
            worker_id = existing.get("worker_id") or self.campaign_store.primary_worker_id(existing["job_id"], key)
            existing["worker_id"] = worker_id
            status = existing.get("status")
            if status == JOB_USER_STOPPED:
                now = self.business_hours.now()
                if self.business_hours.is_send_allowed(now):
                    self.campaign_store.set_status(existing["job_id"], JOB_RUNNING, now=now)
                    if worker_id:
                        self.campaign_store.set_worker_status(worker_id, JOB_RUNNING, now=now, sync_job=False)
                else:
                    nxt = self.business_hours.next_send_window_start(now).isoformat(timespec="seconds")
                    self.campaign_store.set_status(
                        existing["job_id"],
                        JOB_SCHEDULED_PAUSE,
                        next_resume_at=nxt,
                        now=now,
                    )
                    if worker_id:
                        self.campaign_store.set_worker_status(
                            worker_id,
                            JOB_SCHEDULED_PAUSE,
                            next_resume_at=nxt,
                            now=now,
                            sync_job=False,
                        )
                existing = self.campaign_store.get_job(existing["job_id"]) or existing
                existing["worker_id"] = worker_id
            return existing
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                config = json.load(f).get(key)
        except Exception:
            config = None
        all_rows = self._campaign_recipient_rows(key)
        if not config or not all_rows:
            return None
        live = resolve_smtp_for_send(self.config_file, key, config)
        if not live:
            raise RuntimeError("SMTP 계정 자격증명을 찾을 수 없습니다. 계정 설정에서 비밀번호를 확인하세요.")
        attach = {
            "files": list((data or {}).get("files") or []),
            "imgs": dict((data or {}).get("imgs") or {}),
        }
        missing = missing_attachment_paths(attach)
        if missing:
            job = self.campaign_store.create_job(
                login_user_id=self.login_user_id,
                task_key=key,
                provider=p,
                account_idx=i,
                subject=title,
                body=body,
                sender_name=s_name,
                smtp_config=public_smtp_snapshot(config, key),
                interval_label=interval,
                prevent_dup=bool(prevent_dup),
                apply_public_filter=bool(apply_public_filter),
                template_name=_dedup_template_key(template_name, title),
                attachments=attach,
                recipients=all_rows,
                status=JOB_NEEDS_ATTENTION,
                now=self.business_hours.now(),
            )
            if not job.get("joined_existing"):
                self.campaign_store.set_needs_attention(job["job_id"], format_missing_files_reason(missing))
            elif job.get("worker_id"):
                self.campaign_store.set_needs_attention_worker(job["worker_id"], format_missing_files_reason(missing))
            live_job = self.campaign_store.get_job(job["job_id"]) or job
            live_job["worker_id"] = job.get("worker_id")
            live_job["joined_existing"] = job.get("joined_existing")
            return live_job
        job = self.campaign_store.create_job(
            login_user_id=self.login_user_id,
            task_key=key,
            provider=p,
            account_idx=i,
            subject=title,
            body=body,
            sender_name=s_name,
            smtp_config=public_smtp_snapshot(config, key),
            interval_label=interval,
            prevent_dup=bool(prevent_dup),
            apply_public_filter=bool(apply_public_filter),
            template_name=_dedup_template_key(template_name, title),
            attachments=attach,
            recipients=all_rows,
            status=JOB_QUEUED,
            now=self.business_hours.now(),
        )
        if snapshot_contains_secrets(job.get("smtp_config_json")):
            self.campaign_store.scrub_stored_smtp_secrets()
            refreshed = self.campaign_store.get_job(job["job_id"])
            if refreshed:
                refreshed["worker_id"] = job.get("worker_id")
                refreshed["joined_existing"] = job.get("joined_existing")
                job = refreshed
        wid = job.get("worker_id")
        if self.business_hours.is_send_allowed():
            if wid:
                self.campaign_store.set_worker_status(wid, JOB_RUNNING, now=self.business_hours.now())
            if not job.get("joined_existing"):
                self.campaign_store.set_status(job["job_id"], JOB_RUNNING, now=self.business_hours.now())
            elif (self.campaign_store.get_job(job["job_id"]) or {}).get("status") not in RESUME_JOB_STATUSES:
                self.campaign_store.set_status(job["job_id"], JOB_RUNNING, now=self.business_hours.now())
        else:
            nxt = self.business_hours.next_send_window_start()
            iso = nxt.isoformat(timespec="seconds")
            if job.get("joined_existing") and wid:
                self.campaign_store.set_worker_status(
                    wid, JOB_SCHEDULED_PAUSE, next_resume_at=iso, now=self.business_hours.now()
                )
            else:
                self.campaign_store.set_status(
                    job["job_id"],
                    JOB_SCHEDULED_PAUSE,
                    next_resume_at=iso,
                    now=self.business_hours.now(),
                )
                if wid:
                    self.campaign_store.set_worker_status(
                        wid, JOB_SCHEDULED_PAUSE, next_resume_at=iso, now=self.business_hours.now(), sync_job=False
                    )
        live_job = self.campaign_store.get_job(job["job_id"]) or job
        live_job["worker_id"] = wid
        live_job["joined_existing"] = job.get("joined_existing")
        return live_job

    def _start_campaign_runner(self, job_id, key, s_b=None, st_b=None):
        lock = self._engine_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            p, i = self._split_task_key(key)
            self.write_log(p, i, "이미 같은 작업의 발송 루프가 실행 중입니다.")
            return "locked"
        try:
            return self._run_campaign_runner_locked(job_id, key, s_b, st_b)
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    def _run_campaign_runner_locked(self, job_id, key, s_b=None, st_b=None):
        job0 = self.campaign_store.get_job(job_id) or {}
        worker_id = self.campaign_worker_ids.get(key) or job0.get("worker_id") or self.campaign_store.primary_worker_id(job_id, key)
        worker0 = self.campaign_store.get_worker(worker_id) if worker_id else None
        p = (worker0 or {}).get("provider") or job0.get("provider") or self._split_task_key(key)[0]
        try:
            i = int((worker0 or {}).get("account_idx") or job0.get("account_idx") or self._split_task_key(key)[1])
        except Exception:
            i = self._split_task_key(key)[1]
        job0 = self._bind_campaign_job(key, job0 or job_id, worker_id) or job0
        event_generation = int(job0.get("generation") or self.campaign_generations.get(key) or 0)
        self._sync_autostart_registry()
        self._refresh_campaign_ui(key, job_id=job_id, generation=event_generation)
        interval_label = (worker0 or {}).get("interval_label") or job0.get("interval_label") or "5분"

        def prepare(job, item):
            row_data = item.get("recipient") or {}
            if not isinstance(row_data, dict):
                row_data = {}
            title = job.get("subject") or ""
            body = job.get("body") or ""
            s_name = job.get("sender_name") or ""
            actual_template = _dedup_template_key(job.get("template_name"), title)
            prevent_dup = bool(job.get("prevent_dup"))
            apply_public_filter = bool(job.get("apply_public_filter"))
            data = self.campaign_store.job_snapshot_attachments(job)
            live_worker = self.campaign_store.get_worker(worker_id) if worker_id else None
            smtp_key = (live_worker or {}).get("task_key") or key
            snapshot = self.campaign_store.worker_smtp_config(live_worker or {}) if live_worker else self.campaign_store.job_smtp_config(job)
            config = resolve_smtp_for_send(self.config_file, smtp_key, snapshot)
            if not config:
                return "halt", "SMTP 계정 자격증명을 찾을 수 없습니다. 계정 설정에서 비밀번호를 확인한 뒤 다시 시작하세요."
            if snapshot_contains_secrets(snapshot):
                self.campaign_store.scrub_stored_smtp_secrets()
            email = (item.get("email") or row_data.get("이메일") or row_data.get("email") or "").strip()
            comp = item.get("company") or row_data.get("업체명") or row_data.get("comp") or ""
            idx = item.get("seq") or 0
            total = job.get("total_count") or 0
            try:
                if apply_public_filter and check_smart_filter(email, comp):
                    self.write_log(
                        p, i, f"🚫 [{idx}/{total}] 필터링: {comp} <{email}> (공공/단체 규칙 일치로 스킵됨)"
                    )
                    return "skipped", "public_filter"
                bl, bl_email, bl_reason = self._is_blacklisted_detail(email)
                if bl:
                    self.write_log(
                        p,
                        i,
                        f"🚫 [{idx}/{total}] 스킵: {comp} (블랙리스트 차단)",
                    )
                    return "skipped", "blacklist"
                final_title, final_body = self._render_message_with_variables(key, title, body, row_data)
                body_html, _embedded = self._process_body_html(final_body, comp)
                body_hash = _hash_body_html_sha256(body_html)
                if prevent_dup:
                    dup, dup_reason, _ = self.check_duplicate_send_status(email, body_hash, actual_template)
                    if dup and dup_reason in ("same_body_same_sender", "same_template_same_sender"):
                        self.write_log(
                            p,
                            i,
                            f"🚫 [{idx}/{total}] 스킵: {comp} (동일 계정·동일 본문 재발송) <{email}> 「{actual_template}」",
                        )
                        return "skipped", "duplicate"
                msg = self._build_single_mime(config, s_name, email, final_title, final_body, data, comp, message_id=item.get("message_id"))
                return "ready", {
                    "msg": msg,
                    "email": email,
                    "comp": comp,
                    "body_hash": body_hash,
                    "final_title": final_title,
                    "no": idx,
                    "actual_template": actual_template,
                    "message_id": item.get("message_id"),
                    "config": config,
                }
            except Exception as e:
                self.write_log(p, i, f"❌ [{idx}/{total}] {comp} <{email}> MIME 조립 오류: {e}")
                return "error", str(e)

        def send_once(payload, job, item):
            config = payload.get("config") or resolve_smtp_for_send(
                self.config_file, key, self.campaign_store.job_smtp_config(job)
            )
            if not config:
                return False, "자격증명 없음"
            ok, err = self._send_once(config, payload["msg"])
            idx = payload.get("no")
            total = job.get("total_count") or 0
            email, comp = payload.get("email"), payload.get("comp")
            if ok:
                self.write_log(p, i, f"✅ [{idx}/{total}] {comp} <{email}> 성공")
                self.update_last_sent_state(key, idx, comp, email)
                actual_template = payload.get("actual_template") or ""
                final_title = payload.get("final_title") or ""
                body_hash = payload.get("body_hash")
                eff_tpl = self._effective_template_for_log(actual_template, final_title)
                self.record_success_to_db(
                    key, p, i, comp, email, final_title, actual_template, content_hash=body_hash, message_id=payload.get("message_id")
                )
                self.after(0, lambda c=comp, em=email, t=eff_tpl: self._append_cloud_sent_row(c, em, t))
                self._update_stats_label()
                lbl = self.progress_labels.get(key)
                if lbl:
                    at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.after(
                        0,
                        lambda n=idx, c=comp, e=email, a=at: (
                            lbl.configure(text=f"마지막 성공 전송: {n}행  {c} <{e}>  ({a})")
                            if self._campaign_event_is_current(key, job_id, event_generation)
                            else None
                        ),
                    )
            else:
                self.write_log(
                    p, i, f"❌ [{idx}/{total}] {comp} <{email}> 시도 실패: {self._translate_smtp_error(err)}"
                )
            return ok, err

        def on_progress(stats):
            if not self._campaign_event_is_current(key, job_id, event_generation):
                return
            self._refresh_campaign_ui(key, job_id=job_id, generation=event_generation)
            st = (stats or {}).get("status")
            self._set_send_buttons(
                key,
                st,
                job_id=job_id,
                generation=event_generation,
            )

        runner = CampaignRunner(
            self.campaign_store,
            self.business_hours,
            prepare_fn=prepare,
            send_once_fn=send_once,
            is_user_stopped=lambda: bool(self.stop_flags.get(key)),
            is_cancelled=lambda: bool(self.campaign_cancel_flags.get(key)),
            interval_seconds_fn=lambda: self.get_wait_seconds(interval_label),
            on_log=lambda m: self.write_log(p, i, m),
            on_progress=on_progress,
            max_retries=3,
            wait_poll_seconds=1,
            owner=f"{self.login_user_id}:{key}:{os.getpid()}",
            sent_lookup_fn=lambda item, job=job0: self._lookup_sent_for_campaign_item(item, job),
            worker_id=worker_id,
        )
        result = runner.run(job_id, wait_off_hours=True)
        self._refresh_campaign_ui(key, job_id=job_id, generation=event_generation)
        self._sync_autostart_registry()
        if result == JOB_SCHEDULED_PAUSE:
            self._set_send_buttons(key, result, job_id=job_id, generation=event_generation)
        elif result == JOB_NEEDS_ATTENTION:
            self._set_send_buttons(key, result, job_id=job_id, generation=event_generation)
            live = self.campaign_store.get_job(job_id) or {}
            if (
                self._campaign_event_is_current(key, job_id, event_generation)
                and (
                    live.get("status") == JOB_NEEDS_ATTENTION
                    or self.campaign_store.count_by_status(job_id, "needs_review") > 0
                )
            ):
                schedule_on_ui(
                    self,
                    lambda: (
                        self._prompt_needs_attention(self.campaign_store.get_job(job_id))
                        if self._campaign_event_is_current(key, job_id, event_generation)
                        else None
                    ),
                )
        else:
            self._set_send_buttons(key, result, job_id=job_id, generation=event_generation)
            if s_b is not None and st_b is not None:
                if self._campaign_event_is_current(key, job_id, event_generation):
                    self.reset_btns(s_b, st_b)
        return result

    def _recover_campaigns_if_any(self):
        if not self.campaign_store or self._recovery_started:
            return
        self._recovery_started = True
        uid = self.login_user_id or ""
        try:
            attention = self.campaign_store.list_attention_workers(uid)
            workers = self.campaign_store.list_resumable_workers(uid)
        except Exception:
            return
        for worker in attention:
            if (worker.get("login_user_id") or "") != uid:
                continue
            key = worker.get("task_key") or ""
            p, i = self._split_task_key(key)
            self._bind_campaign_job(key, worker["job_id"], worker["worker_id"])
            self.write_log(p, i, f"⚠ 사용자 확인이 필요합니다. {worker.get('attention_reason') or ''}")
            self._set_send_buttons(key, JOB_NEEDS_ATTENTION)
            self._refresh_campaign_ui(key)
            job = self.campaign_store.get_job(worker["job_id"]) or worker
            if (job.get("status") == JOB_NEEDS_ATTENTION):
                schedule_on_ui(self, lambda j=job: self._prompt_needs_attention(j))
        for worker in workers:
            if (worker.get("login_user_id") or "") != uid:
                continue
            key = worker.get("task_key") or ""
            if not key:
                continue
            if key in self._engine_locks and self._engine_locks[key].locked():
                continue
            self.stop_flags[key] = False
            self.campaign_cancel_flags[key] = False
            self._bind_campaign_job(key, worker["job_id"], worker["worker_id"])
            btns = self.campaign_buttons.get(key) or {}
            p, i = self._split_task_key(key)
            self.write_log(p, i, "🔁 저장된 발송 작업을 복구합니다.")
            threading.Thread(
                target=self._start_campaign_runner,
                args=(worker["job_id"], key, btns.get("start"), btns.get("stop")),
                daemon=True,
            ).start()
        self._sync_autostart_registry()

    def _send_once(self, config, msg):
        """SMTP 1회 발송. 연결 끊김 시 즉시 1회 재접속은 같은 시도로 본다."""
        def _connect_send_quit():
            server = smtplib.SMTP_SSL(config["smtp"], int(config["port"]), timeout=20)
            server.login(config["id"], config["pw"])
            server.send_message(msg)
            server.quit()
            return True, "성공"

        def _is_connection_error(e):
            if e is None:
                return False
            err = str(e).strip()
            if "Server not connected" in err or "Connection reset" in err:
                return True
            if isinstance(e, BrokenPipeError):
                return True
            if hasattr(smtplib, "SMTPServerDisconnected") and isinstance(e, smtplib.SMTPServerDisconnected):
                return True
            return False

        server = None
        try:
            server = smtplib.SMTP_SSL(config["smtp"], int(config["port"]), timeout=20)
            server.login(config["id"], config["pw"])
            server.send_message(msg)
            server.quit()
            return True, "성공"
        except (BrokenPipeError, OSError, smtplib.SMTPException) as e:
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            if _is_connection_error(e):
                try:
                    return _connect_send_quit()
                except Exception as e2:
                    return False, str(e2)
            return False, str(e)
        except Exception as e:
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass
            return False, str(e)

    def _send_with_retry(self, config, msg, max_retries=3):
        """최대 3회 재시도. Task 1-3: Server not connected / BrokenPipeError 시 연결 해제 후 1회 즉시 재접속 발송."""
        def _connect_send_quit():
            server = smtplib.SMTP_SSL(config['smtp'], int(config['port']), timeout=20)
            server.login(config['id'], config['pw'])
            server.send_message(msg)
            server.quit()
            return True, "성공"

        def _is_connection_error(e):
            if e is None:
                return False
            err = str(e).strip()
            if "Server not connected" in err or "Connection reset" in err:
                return True
            if isinstance(e, BrokenPipeError):
                return True
            if hasattr(smtplib, 'SMTPServerDisconnected') and isinstance(e, smtplib.SMTPServerDisconnected):
                return True
            return False

        connection_error_retried = False
        for attempt in range(1, max_retries + 1):
            server = None
            try:
                server = smtplib.SMTP_SSL(config['smtp'], int(config['port']), timeout=20)
                server.login(config['id'], config['pw'])
                server.send_message(msg)
                server.quit()
                return True, "성공"
            except (BrokenPipeError, OSError, smtplib.SMTPException) as e:
                if server is not None:
                    try:
                        server.close()
                    except Exception:
                        pass
                if _is_connection_error(e) and not connection_error_retried:
                    connection_error_retried = True
                    try:
                        ok, _ = _connect_send_quit()
                        if ok:
                            return True, "성공"
                    except Exception as e2:
                        e = e2
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return False, str(e)
            except Exception as e:
                if server is not None:
                    try:
                        server.close()
                    except Exception:
                        pass
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return False, str(e)
        return False, "알 수 없는 오류"

    def real_engine(self, p, i, title, body, s_name, data, interval, prevent_dup, apply_public_filter, tree, s_b, st_b, template_name=""): #
        key = f"{p}_{i}"
        bound_job_id = ""
        bound_generation = 0
        self.stop_flags[key] = False
        self.campaign_cancel_flags[key] = False
        self._set_send_buttons(key, "running")
        try:
            job = self._ensure_campaign_job(
                p, i, title, body, s_name, data, interval, prevent_dup, apply_public_filter, template_name
            )
            if not job:
                self.write_log(p, i, "❌ 계정/수신처 부족")
                return
            self._bind_campaign_job(key, job, job.get("worker_id"))
            bound_job_id = job["job_id"]
            bound_generation = int(job.get("generation") or 0)
            if job.get("status") == JOB_NEEDS_ATTENTION:
                self.write_log(p, i, job.get("attention_reason") or "사용자 확인이 필요합니다.")
                self._set_send_buttons(
                    key,
                    JOB_NEEDS_ATTENTION,
                    job_id=bound_job_id,
                    generation=bound_generation,
                )
                schedule_on_ui(
                    self,
                    lambda j=job: (
                        self._prompt_needs_attention(j)
                        if self._campaign_event_is_current(key, bound_job_id, bound_generation)
                        else None
                    ),
                )
                return
            if job.get("status") == JOB_SCHEDULED_PAUSE:
                nxt = job.get("next_resume_at")
                try:
                    dt = datetime.fromisoformat(nxt) if nxt else self.business_hours.next_send_window_start()
                except Exception:
                    dt = self.business_hours.next_send_window_start()
                self.write_log(p, i, self.business_hours.format_resume_text(dt))
                self._set_send_buttons(
                    key,
                    JOB_SCHEDULED_PAUSE,
                    job_id=bound_job_id,
                    generation=bound_generation,
                )
            self._start_campaign_runner(job["job_id"], key, s_b, st_b)
        except StorageWriteError as exc:
            self._notify_storage_error(exc)
        except DuplicateActiveCampaignError as e:
            self.write_log(p, i, "❌ 이미 이 SMTP 계정으로 실행 중인 자동발송이 있습니다.")
            schedule_on_ui(
                self,
                lambda: messagebox.showwarning(
                    "캠페인 중복",
                    "이미 이 SMTP 계정으로 진행 중이거나 예약 대기 중인 자동발송이 있습니다.\n"
                    "같은 계정은 동시에 두 번 시작할 수 없습니다.",
                    parent=self,
                ),
            )
        except Exception as e:
            self.write_log(p, i, f"❌ 작업 오류: {e}")
        finally:
            self._start_in_flight[key] = False
            job = self.campaign_store.get_job(bound_job_id) if bound_job_id else None
            st = (job or {}).get("status") or ""
            self._set_send_buttons(
                key,
                st,
                job_id=bound_job_id if bound_job_id else None,
                generation=bound_generation if bound_job_id else None,
            )
            if st in (JOB_COMPLETED, JOB_CANCELLED, "") and (
                not bound_job_id
                or self._campaign_event_is_current(key, bound_job_id, bound_generation)
            ):
                self.reset_btns(s_b, st_b)
            self._sync_autostart_registry()
            self._refresh_campaign_ui(
                key,
                job_id=bound_job_id if bound_job_id else None,
                generation=bound_generation if bound_job_id else None,
            )

    def write_log(self, p, i, m):
        key = f"{p}_{i}"
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {m}"
        bucket = self._log_buffers.setdefault(key, deque(maxlen=LOG_CONSOLE_MAX_LINES))
        bucket.append(line)

        def _apply():
            if key != self._live_task_key():
                return
            self._render_log(key)

        schedule_on_ui(self, _apply)

    def set_stop(self, k):
        self.stop_flags[k] = True
        job_id = self.campaign_job_ids.get(k)
        generation = self.campaign_generations.get(k)
        if job_id and self.campaign_store:
            worker_id = self.campaign_worker_ids.get(k) or self.campaign_store.primary_worker_id(job_id, k)
            now = self.business_hours.now()
            self.campaign_store.set_status(job_id, JOB_USER_STOPPED, now=now, clear_runner=True)
            if worker_id:
                self.campaign_store.set_worker_status(
                    worker_id,
                    JOB_USER_STOPPED,
                    now=now,
                    clear_runner=True,
                    sync_job=False,
                )
            self._set_send_buttons(
                k,
                JOB_USER_STOPPED,
                job_id=job_id,
                generation=generation,
            )
            self._refresh_campaign_ui(k, job_id=job_id, generation=generation)
        p, i = self._split_task_key(k)
        self.write_log(p, i, "⏹ 사용자 요청으로 이 계정만 정지합니다. 다른 계정은 계속 발송됩니다.")

    def set_cancel(self, k):
        self.campaign_cancel_flags[k] = True
        self.stop_flags[k] = True
        job_id = self.campaign_job_ids.get(k)
        generation = self.campaign_generations.get(k)
        if job_id and self.campaign_store:
            try:
                cancel_campaign(self.campaign_store, job_id, now=self.business_hours.now())
            except Exception:
                pass
            self._set_send_buttons(
                k,
                JOB_CANCELLED,
                job_id=job_id,
                generation=generation,
            )
            self._refresh_campaign_ui(k, job_id=job_id, generation=generation)
        p, i = self._split_task_key(k)
        self.write_log(p, i, "🗑 선택 계정의 작업을 취소했습니다. 다른 계정은 계속 발송됩니다.")
    def reset_btns(self, s, st): self.after(0, lambda: (s.configure(state="normal"), st.configure(state="disabled", fg_color="#555")))

    def _export_to_excel(self, task_key):
        """발송 결과를 엑셀로 내보내기"""
        if not task_key:
            messagebox.showerror("오류", "작업 키가 없습니다.")
            return

        try:
            import pandas as pd
        except ImportError:
            messagebox.showerror("패키지 없음", "pandas가 설치되어 있지 않아 엑셀 내보내기를 할 수 없습니다.\n\npip install pandas openpyxl")
            return

        con = sqlite3.connect(self.db_path)
        try:
            df = pd.read_sql_query(
                "SELECT id, provider, account_idx as account, comp, email, subject, template_name, content_hash, sent_at FROM sent_log WHERE task_key=? ORDER BY sent_at DESC",
                con,
                params=(task_key,)
            )
        finally:
            con.close()

        if df.empty:
            messagebox.showinfo("알림", f"발송 결과가 없습니다.")
            return

        # 엑셀 파일 저장 경로 선택
        file_path = self.dialogs.asksaveasfilename(
            key="export_excel",
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx"), ("All Files", "*.*")],
            initialfile=f"{task_key}_발송결과_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        )

        if not file_path:
            return

        try:
            df.to_excel(file_path, index=False, sheet_name="발송 결과")
            messagebox.showinfo("완료", f"엑셀 파일이 저장되었습니다:\n{file_path}")
        except Exception as e:
            messagebox.showerror("오류", f"엑셀 파일 저장 중 오류가 발생했습니다:\n{e}")

    def _open_blacklist_manager(self):
        """블랙리스트 관리 창 열기"""
        if BlacklistManager is None:
            messagebox.showerror("오류", "블랙리스트 관리 모듈을 찾을 수 없습니다.")
            return
        key = "blacklist"
        existing = self.dialogs.get_open(key)
        if existing not in (None, True, "native") and is_widget_alive(existing):
            self.dialogs.reveal_toplevel(existing, modal=False, key=key)
            return
        parent = self.dialogs.live_parent() or self
        win = BlacklistManager(parent, self)
        win._dialog_close_cb = lambda: self._close_managed_window(win, key)
        self.dialogs.register_open(key, win)
        self.dialogs.reveal_toplevel(win, modal=False, key=key)

    def _sync_blacklist_from_sheet(self):
        """Phase 5 Task 5-1: 구글 시트 'blacklist' 워크시트에서 읽어 로컬 blacklist 테이블 최신화.
        시트 1행=공지, 2행=헤더, 3행부터 데이터. B열(인덱스1)=업체명, C열(인덱스2)=이메일.
        반환: (성공 여부, 성공 시 동기화 건수 / 실패 시 오류 메시지)"""
        if gspread is None:
            return False, "gspread가 설치되어 있지 않습니다. pip install gspread google-auth"
        cred_path = bundled_file("credentials.json")
        if not os.path.exists(cred_path):
            return False, "credentials.json을 찾을 수 없습니다."
        try:
            client = gspread.service_account(filename=cred_path)
            spreadsheet = client.open_by_key(BLACKLIST_SHEET_KEY)
            try:
                ws = spreadsheet.worksheet("blacklist")
            except gspread.WorksheetNotFound:
                return False, "구글 시트에 'blacklist' 시트가 없습니다."
            all_values = ws.get_all_values()
            # 3행(Python 인덱스 2)부터 데이터
            rows = all_values[2:] if len(all_values) > 2 else []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            to_insert = []
            for row in rows:
                comp = (row[1].strip() if len(row) > 1 else "") or ""
                email = (row[2].strip() if len(row) > 2 else "") or ""
                if not email:
                    continue
                to_insert.append((email, comp, "시트 동기화", now))
            con = sqlite3.connect(self.db_path)
            try:
                con.execute("DELETE FROM blacklist")
                con.executemany(
                    "INSERT OR IGNORE INTO blacklist(email, comp, reason, added_at) VALUES(?,?,?,?)",
                    to_insert,
                )
                con.commit()
                return True, len(to_insert)
            finally:
                con.close()
        except Exception as e:
            return False, str(e)

    def get_wait_seconds(self, interval_label):
        if "랜덤" in interval_label:
            return random.randint(1, 10) * 60
        mapping = {"1분": 60, "2분": 120, "3분": 180, "5분": 300, "10분": 600}
        return mapping.get(interval_label, 300)

    def _process_body_html(self, body_html, comp):
        # Replace placeholder and convert embedded base64 images to CID attachments.
        html = str(body_html or "").replace("{업체명}", comp)
        embedded = []

        def _replace(match):
            data_url = match.group(1)
            m = re.match(r"data:(image/[^;]+);base64,(.+)", data_url, re.I)
            if not m:
                return match.group(0)
            mime, b64 = m.group(1), m.group(2)
            try:
                img_data = base64.b64decode(b64)
            except Exception:
                return match.group(0)
            subtype = mime.split("/", 1)[1] if "/" in mime else "jpeg"
            cid = f"embed_{len(embedded)+1}"
            embedded.append((cid, img_data, subtype))
            return f'src="cid:{cid}"'

        new_html = re.sub(r'src=["\'](data:image/[^"\']+)["\']', _replace, html, flags=re.I)
        return new_html, embedded

    def _resolve_from_address(self, config):
        """Phase 9 (v2.7.3): auth_type에 따라 msg['From']에 쓸 발신 주소를 결정.
        - standard(구버전): 로그인 아이디(config['id'])를 그대로 From 주소로 사용.
        - separated(신버전, Amazon SES 등): sender_email을 From 주소로 사용.
        하위호환: auth_type 키가 없으면 standard로 간주.
        로그인(server.login)은 항상 config['id']/config['pw']를 사용하므로 별도 처리한다.
        """
        auth_type = str((config or {}).get("auth_type", "standard") or "standard").strip().lower()
        if auth_type == "separated":
            sender_email = str((config or {}).get("sender_email", "") or "").strip()
            if sender_email:
                return sender_email
        return config.get("id", "")

    def _build_single_mime(self, config, s_name, to_email, final_title, final_body, data, comp, message_id=None):
        """서버 접속 없이 MIME 메시지 1통 조립. `real_engine`에서 1건씩 조립 후 즉시 발송."""
        msg = MIMEMultipart()
        msg['From'] = formataddr((str(Header(s_name or "운영사무국", 'utf-8')), self._resolve_from_address(config)))
        msg['To'] = to_email
        msg['Subject'] = Header(final_title, 'utf-8')
        if message_id:
            msg['Message-ID'] = message_id
        body_html, embedded_imgs = self._process_body_html(final_body, comp)
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        for cid, img_data, subtype in embedded_imgs:
            img = MIMEImage(img_data, _subtype=subtype)
            img.add_header('Content-ID', f'<{cid}>')
            msg.attach(img)
        for cid, path in data["imgs"].items():
            with open(path, 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', f'<{cid}>')
                msg.attach(img)
        for path in data["files"]:
            fn = os.path.basename(path)
            ct, _ = mimetypes.guess_type(path)
            if ct is None:
                ct = 'application/octet-stream'
            main, sub = ct.split('/', 1)
            with open(path, 'rb') as f:
                part = MIMEBase(main, sub)
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', fn))
                msg.attach(part)
        return msg

    def _open_wysiwyg_editor(self, initial_html):
        if webview is None:
            messagebox.showerror("의존성 없음", "pywebview가 설치되어 있지 않습니다.\n\npip install pywebview")
            return None

        class _EditorApi:
            def __init__(self, initial):
                self.initial = initial or ""
                self.result = None
                self._event = threading.Event()
                self._window = None

            def getInitialContent(self):
                return self.initial

            def saveContent(self, html):
                self.result = html
                self._event.set()
                if self._window:
                    try:
                        webview.destroy_window(self._window)
                    except Exception:
                        pass
                return True

            def notifyClosed(self):
                # Window closed by user; signal waiting thread.
                self._event.set()
                return True

        api = _EditorApi(initial_html)
        # 패키징(onefile) 시 번들된 HTML은 _MEIPASS에 추출됨
        editor_base = getattr(sys, "_MEIPASS", BASE_DIR)
        html_path = os.path.join(editor_base, "wysiwyg_editor.html")

        def _run():
            try:
                window = webview.create_window("템플릿 에디터", html_path, js_api=api, width=900, height=700, resizable=True)
                api._window = window
                # Ensure waiter is released even if user closes window directly
                try:
                    window.events.closed += lambda: api.notifyClosed()
                except Exception:
                    pass
                webview.start()
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("에디터 오류", f"에디터를 열 수 없습니다:\n{e}"))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        api._event.wait(timeout=900)
        return api.result

    def _open_editor_for_body(self, body_widget):
        html = body_widget.get("1.0", "end-1c")
        def _run():
            new_html = self._open_wysiwyg_editor(html)
            if new_html is not None:
                self.after(0, lambda: (body_widget.delete("1.0", "end"), body_widget.insert("1.0", new_html)))
        threading.Thread(target=_run, daemon=True).start()

    def _start_test_send(self, provider, idx, title, body, sender_name, data, send_btn, test_btn):
        to_email = self.dialogs.askstring("테스트 메일", "테스트 수신처 이메일:", key="test_mail")
        if not to_email:
            return

        send_btn.configure(state="disabled")
        test_btn.configure(state="disabled")

        def _run():
            key = f"{provider}_{idx}"
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f).get(key)
                if not config:
                    self.write_log(provider, idx, "❌ 계정 정보가 없습니다.")
                    return
                profile = self.get_login_user_profile()
                resolved_sender_name = (sender_name or "").strip()
                if not resolved_sender_name:
                    resolved_sender_name = (profile.get("user_name") or "").strip() or (self.user_name or "").strip()
                sample_row = {"업체명": "테스트", "이메일": to_email}
                final_title, final_body = self._render_message_with_variables(key, title, body, sample_row)

                msg = MIMEMultipart()
                msg['From'] = formataddr((str(Header(resolved_sender_name or "운영사무국", 'utf-8')), self._resolve_from_address(config)))
                msg['To'] = to_email
                msg['Subject'] = Header(final_title, 'utf-8')

                body_html, embedded_imgs = self._process_body_html(final_body, "테스트")
                msg.attach(MIMEText(body_html, 'html', 'utf-8'))
                for cid, img_data, subtype in embedded_imgs:
                    img = MIMEImage(img_data, _subtype=subtype)
                    img.add_header('Content-ID', f'<{cid}>')
                    msg.attach(img)

                for cid, path in data["imgs"].items():
                    with open(path, 'rb') as f:
                        img = MIMEImage(f.read()); img.add_header('Content-ID', f'<{cid}>'); msg.attach(img)
                for path in data["files"]:
                    fn = os.path.basename(path); ct, _ = mimetypes.guess_type(path)
                    if ct is None: ct = 'application/octet-stream'
                    main, sub = ct.split('/', 1)
                    with open(path, 'rb') as f:
                        part = MIMEBase(main, sub); part.set_payload(f.read()); encoders.encode_base64(part)
                        part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', fn)); msg.attach(part)

                server = smtplib.SMTP_SSL(config['smtp'], int(config['port']), timeout=20)
                server.login(config['id'], config['pw'])
                server.send_message(msg)
                server.quit()
                self.write_log(provider, idx, f"🧪 테스트 발송 완료: {to_email}")
            except Exception as e:
                self.write_log(provider, idx, f"❌ 테스트 발송 오류: {self._translate_smtp_error(str(e))}")
            finally:
                self.after(0, lambda: (send_btn.configure(state="normal"), test_btn.configure(state="normal")))

        threading.Thread(target=_run, daemon=True).start()

    def attach_file(self, d, refresh=None):
        """선택한 파일을 첨부 목록에 추가(기존 목록에 이어 붙임, 동일 경로는 제외)."""
        ps = self.dialogs.askopenfilenames(key="attach_files", title="첨부파일 선택")
        if not ps:
            return
        cur = list(d.get("files") or [])
        seen = {os.path.normcase(os.path.abspath(str(x))) for x in cur}
        for p in ps:
            ap = os.path.abspath(str(p))
            nc = os.path.normcase(ap)
            if nc not in seen:
                cur.append(p)
                seen.add(nc)
        d["files"] = cur
        if callable(refresh):
            refresh()

    def attach_cid(self, d, refresh=None):
        p = self.dialogs.askopenfilename(key="attach_cid_file", title="CID 이미지 선택")
        if not p:
            return
        c = self.dialogs.askstring("CID", "CID 이름:", key="attach_cid_name")
        if not c or not str(c).strip():
            return
        c = str(c).strip()
        if not isinstance(d.get("imgs"), dict):
            d["imgs"] = {}
        d["imgs"][c] = p
        if callable(refresh):
            refresh()

    def save_tpl(self, t, b, s, d, task_key=None):
        n = self.dialogs.askstring("저장", "템플릿 이름:", key="save_template_name")
        if n:
            try:
                with open(self.template_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[n] = {
                "title": t.get(),
                "body": b.get("1.0", "end-1c"),
                "sender": s.get(),
                "files": list(d.get("files") or []),
                "imgs": dict(d.get("imgs") or {}),
            }
            data, _changed = ensure_template_ids(data)
            self._atomic_write_json_path(self.template_file, data, indent=4, ensure_ascii=False)
            messagebox.showinfo("완료", f"'{n}' 저장 성공")
            if task_key:
                self._remember_template(task_key, n)

    def open_tpl_library(self, t, b, s, d, f_l, i_l, task_key_tpl, refresh_attach=None):
        key = "template_library"
        pop = self.dialogs.open_toplevel(
            key,
            title="템플릿",
            geometry="320x420",
            minsize=(260, 280),
            modal=False,
        )
        if pop is None:
            return
        if getattr(pop, "_ui_dialog_built", False):
            return
        pop._ui_dialog_built = True
        frame = ctk.CTkScrollableFrame(pop)
        frame.pack(fill="both", expand=True, padx=5, pady=5)
        tpls = self._load_templates()
        for name in tpls.keys():
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=1)

            def apply(n=name):
                try:
                    with open(self.template_file, "r", encoding="utf-8") as f:
                        tpl = json.load(f).get(n)
                except Exception:
                    tpl = None
                if not tpl:
                    messagebox.showerror("오류", "템플릿을 읽을 수 없습니다.", parent=pop)
                    return
                t.delete(0, "end")
                t.insert(0, tpl["title"])
                b.delete("1.0", "end")
                b.insert("1.0", tpl["body"])
                s.delete(0, "end")
                s.insert(0, tpl.get("sender", ""))
                d["files"] = []
                d["imgs"] = {}
                raw_files = tpl.get("files")
                raw_imgs = tpl.get("imgs")
                if isinstance(raw_files, list):
                    d["files"] = [str(x) for x in raw_files]
                if isinstance(raw_imgs, dict):
                    d["imgs"] = {str(k): str(v) for k, v in raw_imgs.items()}
                nf, ni = len(d["files"]), len(d["imgs"])
                f_l.configure(text=f"📎 첨부: {nf}개" if nf else "📎 첨부: 없음")
                i_l.configure(text=f"🖼️ CID: {ni}개" if ni else "🖼️ CID: 없음")
                if callable(refresh_attach):
                    refresh_attach()
                self._remember_template(task_key_tpl, n)
                self._close_managed_window(pop, key)

            ctk.CTkButton(row, text=name, command=apply).pack(side="left", expand=True, fill="x", padx=1)
            ctk.CTkButton(row, text="X", width=25, fg_color="red", command=lambda n=name: self.del_tpl(n, pop)).pack(side="right")

    def del_tpl(self, n, p):
        if messagebox.askyesno("삭제", f"'{n}' 삭제할까요?", parent=p):
            try:
                with open(self.template_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            if isinstance(data, dict) and n in data:
                removed = data.get(n) if isinstance(data.get(n), dict) else {}
                del data[n]
                self._atomic_write_json_path(self.template_file, data, indent=4, ensure_ascii=False)
                if self.campaign_store and removed.get("id"):
                    self.campaign_store.clear_template_preferences(removed.get("id"))
            self._close_managed_window(p, "template_library")