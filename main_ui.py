import customtkinter as ctk
from tkinter import ttk, filedialog, messagebox, simpledialog
import json, os, sys, random, sqlite3, re, base64
import smtplib, threading, time, mimetypes
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

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _dedup_template_key(template_name, title_fallback=""):
    """Task 2-2: 중복 검사·sent_log에 기록하는 템플릿 키. 저장된 템플릿명 우선, 없으면 제목. 양쪽 strip()."""
    tn = (template_name or "").strip()
    if tn:
        return tn
    return (title_fallback or "").strip()


class ModernMailSender(ctk.CTk):
    def __init__(self, user_name="사용자", grade="무료권", remaining="0"):
        super().__init__()
        self.user_name, self.grade, self.remaining = user_name, grade, remaining
        self.config_file = os.path.join(BASE_DIR, "config.json")
        self.template_file = os.path.join(BASE_DIR, "templates.json")
        self.recipients_file = os.path.join(BASE_DIR, "recipients.json")
        self.db_path = os.path.join(BASE_DIR, "sent_history.db")
        self.log_consoles, self.stop_flags, self.tree_views, self.progress_labels = {}, {}, {}, {}
        self.current_template_name = {}
        self.icon_filename = "pro.ico"
        
        try:
            from login import CURRENT_VERSION
        except ImportError:
            CURRENT_VERSION = "v2.5.2"
        self.title(f"MAIL MONSTER PRO {CURRENT_VERSION}")
        self.geometry("980x686") # 💡 30% 축소 사이즈 적용
        ctk.set_appearance_mode("dark")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        for f in [self.config_file, self.template_file]:
            if not os.path.exists(f): 
                with open(f, 'w', encoding='utf-8') as file: json.dump({}, file)
        if not os.path.exists(self.recipients_file):
            with open(self.recipients_file, 'w', encoding='utf-8') as file: json.dump({}, file, ensure_ascii=False, indent=2)
        self.init_db()
        self.setup_ui()
        # Phase 3 Task 3-1: 구글 시트 '발송내역' → 로컬 DB 동기화 (비동기)
        threading.Thread(target=self._run_startup_sent_log_sync, daemon=True).start()

    def init_db(self):
        con = sqlite3.connect(self.db_path)
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

            con.execute("CREATE INDEX IF NOT EXISTS idx_sent_task_email ON sent_log(task_key, email)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sent_email_template ON sent_log(email, template_name)")

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
        finally:
            con.close()

    def _read_recipients_state_all(self):
        try:
            with open(self.recipients_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_recipients_state_all(self, data):
        with open(self.recipients_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_recipients_state(self, task_key):
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
        return state

    def save_recipients_rows(self, task_key, rows, headers=None):
        data = self._read_recipients_state_all()
        state = data.get(task_key) or {}
        if isinstance(state, list):
            state = {"rows": state, "last_sent": {}, "headers": []}
        if not isinstance(state, dict):
            state = {}
        state.setdefault("last_sent", {})
        state["rows"] = rows
        if headers:
            state["headers"] = headers
        data[task_key] = state
        self._write_recipients_state_all(data)

    def update_last_sent_state(self, task_key, no, comp, email):
        data = self._read_recipients_state_all()
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
        self._write_recipients_state_all(data)

    def _effective_template_for_log(self, template_name, subject):
        """Task 4-4: template_name이 비어 있으면 제목으로 대체해 DB·시트·중복키가 빈 문자열이 되지 않게 함."""
        t = (template_name or "").strip()
        if t:
            return t
        return ((subject or "").strip()[:500] or "(미지정)")

    def record_success_to_db(self, task_key, provider, account_idx, comp, email, subject, template_name=""):
        # 수신처 불명확/실패/미전송은 저장하지 않음: 성공했을 때만 호출
        if not comp:
            return
        e = (email or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
            return
        tpl = self._effective_template_for_log(template_name, subject)
        sender = (self.user_name or "").strip()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO sent_log(task_key, provider, account_idx, comp, email, subject, template_name, sent_at, sender) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_key,
                    provider,
                    int(account_idx),
                    comp,
                    e,
                    subject,
                    tpl,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    sender,
                ),
            )
            con.commit()
        finally:
            con.close()

    def check_duplicate_send_status(self, email, template_name=""):
        """Phase 3~4: 이메일별 sent_log 이력으로 2중 필터(Task 4-3과 동일 규칙).
        반환: (스킵 여부, 사유) — 사유는 'other_sender' | 'same_template' | None
        - 타 담당자(sender가 비어 있지 않고 현재 user_name과 다름)가 한 명이라도 있으면 차단
        - 그렇지 않으면, 본인·레거시(sender 비어 있음) 이력 중 동일 템플릿이 있으면 차단
        """
        e = (email or "").strip()
        if not e:
            return False, None
        tpl = (template_name or "").strip()
        me = (self.user_name or "").strip().lower()
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(
                "SELECT sender, template_name FROM sent_log WHERE email=? COLLATE NOCASE",
                (e,),
            )
            rows = cur.fetchall()
        finally:
            con.close()
        if not rows:
            return False, None
        for sender, tmpl_db in rows:
            s = (sender or "").strip()
            if s and s.lower() != me:
                return True, "other_sender"
        for sender, tmpl_db in rows:
            s = (sender or "").strip()
            if s and s.lower() != me:
                continue
            t = (tmpl_db or "").strip()
            if tpl.lower() == t.lower():
                return True, "same_template"
        return False, None

    def _ensure_sent_log_worksheet(self, spreadsheet):
        """Phase 3: 워크시트 '발송내역'이 없으면 헤더와 함께 생성."""
        if gspread is None:
            return None
        try:
            return spreadsheet.worksheet("발송내역")
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title="발송내역", rows=3000, cols=5)
            ws.append_row(["보낸 날짜", "발송담당자", "업체명", "이메일", "템플릿명"])
            return ws

    def _sync_sent_log_from_sheet(self):
        """Task 3-1: 구글 시트 '발송내역' 전체를 읽어 로컬 sent_log에 없는 행만 삽입."""
        if gspread is None:
            return 0
        cred_path = os.path.join(BASE_DIR, "credentials.json")
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
                if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                    continue
                if not sent_at:
                    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = con.execute(
                    """SELECT 1 FROM sent_log WHERE email=? COLLATE NOCASE AND sent_at=?
                       AND TRIM(COALESCE(sender,''))=? AND LOWER(TRIM(COALESCE(template_name,'')))=LOWER(?) LIMIT 1""",
                    (email, sent_at, sender, tmpl),
                )
                if cur.fetchone():
                    continue
                con.execute(
                    """INSERT INTO sent_log(task_key, provider, account_idx, comp, email, subject, template_name, sent_at, sender)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
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
                    ),
                )
                inserted += 1
            con.commit()
        finally:
            con.close()
        return inserted

    def _append_cloud_sent_row(self, comp, email, template_name):
        """Task 3-3: 발송 성공 시 구글 시트 '발송내역'에 한 줄 추가."""
        if gspread is None:
            return
        cred_path = os.path.join(BASE_DIR, "credentials.json")
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
        e = (email or "").strip()
        if not e:
            return False
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute("SELECT 1 FROM blacklist WHERE email=? COLLATE NOCASE LIMIT 1", (e,))
            return cur.fetchone() is not None
        finally:
            con.close()

    def _add_blacklist(self, email, comp="", reason=""):
        """블랙리스트에 이메일 추가 (Task 5-1)"""
        e = (email or "").strip()
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
        e = (email or "").strip()
        if not e:
            return False
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("DELETE FROM blacklist WHERE email=? COLLATE NOCASE", (e,))
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
        if res is True: self.withdraw(); threading.Thread(target=self.run_tray, daemon=True).start()
        elif res is False: self.destroy(); os._exit(0)

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

    def setup_ui(self):
        self._font_title = ("맑은 고딕", 14, "bold")
        self._font_body = ("맑은 고딕", 12)
        self._font_small = ("맑은 고딕", 11)
        theme_color = "#8e44ad" if "관리자" in self.grade else ("#27ae60" if self.grade == "무료권" else "#2980b9")
        header = ctk.CTkFrame(self, height=60, fg_color="#1a1a1a", corner_radius=0); header.pack(fill="x", side="top")
        
        # 좌측: 로고
        try:
            from login import CURRENT_VERSION as _ver
        except ImportError:
            _ver = "v2.5.2"
        ctk.CTkLabel(header, text=f"🚀 MAIL MONSTER PRO {_ver}", font=("맑은 고딕", 18, "bold"), text_color=theme_color).pack(side="left", padx=20, pady=5)
        
        # 중앙: 통계 라벨
        self.stats_label = ctk.CTkLabel(header, text="📊 오늘 발송: 0건 | 누적 발송: 0건", font=self._font_small, text_color="#e74c3c")
        self.stats_label.pack(side="left", padx=20)
        self._update_stats_label()
        
        # 우측: 사용자 이름
        ctk.CTkLabel(header, text=f"✨ {self.user_name} 님", font=self._font_body).pack(side="right", padx=20, pady=5)
        
        # 우측: Phase 5 Task 5-2 차단 목록 최신화 + 블랙리스트 관리
        def _run_sync_blacklist():
            def worker():
                ok, result = self._sync_blacklist_from_sheet()
                if ok:
                    self.after(0, lambda: messagebox.showinfo("동기화 완료", f"{result}건의 차단 목록이 동기화되었습니다."))
                else:
                    self.after(0, lambda: messagebox.showerror("동기화 실패", str(result)))
            threading.Thread(target=worker, daemon=True).start()
        ctk.CTkButton(header, text="🔄 차단 목록 최신화", width=140, height=35, font=self._font_small, fg_color="#2980b9", command=_run_sync_blacklist).pack(side="right", padx=5, pady=5)
        ctk.CTkButton(header, text="⚙️ 블랙리스트", width=120, height=35, font=self._font_small, command=self._open_blacklist_manager).pack(side="right", padx=5, pady=5)
        
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

        ctk.CTkLabel(sidebar, text="📋 계정 목록", font=self._font_title).pack(pady=12, padx=12, anchor="w")
        scrollable = ctk.CTkScrollableFrame(sidebar, fg_color="transparent")
        scrollable.pack(fill="both", expand=True, padx=8, pady=4)
        self.sidebar_scrollable = scrollable

        self.profile_frames = {}
        self.sidebar_buttons = {}
        self.task_key_to_index = {}
        max_acc = 10 if self.grade != "무료권" else 1
        providers = ["네이버", "다음", "지메일", "네이트", "외부메일"]
        all_slots = [(p, i) for p in providers for i in range(1, max_acc + 1)]
        self._all_task_keys_ordered = [f"{p}_{i}" for p, i in all_slots]

        main_content = ctk.CTkFrame(body, fg_color="transparent")
        main_content.pack(side="left", fill="both", expand=True)
        self.main_content_frame = main_content

        # 모든 슬롯에 대해 content frame 생성 (계정 추가 시 빈 슬롯 표시용)
        for provider, idx in all_slots:
            task_key = f"{provider}_{idx}"
            content_frame = ctk.CTkFrame(main_content, fg_color="transparent")
            self.profile_frames[task_key] = content_frame
            self.build_account_detail(content_frame, provider, idx)

        self._rebuild_sidebar_buttons()
        first_key = self._get_first_display_key()
        self.current_profile = first_key
        if first_key and first_key in self.profile_frames:
            self.profile_frames[first_key].pack(fill="both", expand=True)

    def _get_configured_task_keys(self):
        """config.json에 id가 있는 task_key만 정렬된 리스트로 반환 (스택형 1,2,3...용)"""
        ordered = getattr(self, "_all_task_keys_ordered", [])
        if not ordered:
            return []
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return [tk for tk in ordered if config.get(tk) and config.get(tk).get("id")]
        except Exception:
            return []

    def _get_first_empty_task_key(self):
        """설정되지 않은 첫 번째 슬롯의 task_key"""
        ordered = getattr(self, "_all_task_keys_ordered", [])
        configured = set(self._get_configured_task_keys())
        for tk in ordered:
            if tk not in configured:
                return tk
        return None

    def _get_first_display_key(self):
        """처음 표시할 키: 설정된 계정이 있으면 첫 번째, 없으면 첫 빈 슬롯"""
        configured = self._get_configured_task_keys()
        if configured:
            return configured[0]
        return self._get_first_empty_task_key()

    def _rebuild_sidebar_buttons(self):
        """사이드바 버튼을 설정된 계정만 1부터 스택으로 다시 그림"""
        scrollable = getattr(self, "sidebar_scrollable", None)
        if not scrollable:
            return
        for w in scrollable.winfo_children():
            w.destroy()
        configured = self._get_configured_task_keys()
        self.task_key_to_index.clear()
        self.sidebar_buttons.clear()
        for n, task_key in enumerate(configured, 1):
            self.task_key_to_index[task_key] = n
            label = f"계정 {n}"
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    d = json.load(f).get(task_key)
                    if d and d.get("id"):
                        label = f"계정 {n} ({d['id']})"
            except Exception:
                pass
            btn = ctk.CTkButton(
                scrollable, text=label,
                command=lambda tk=task_key: self._switch_profile(tk),
                fg_color="transparent", anchor="w", height=36,
                font=self._font_small,
            )
            btn.pack(fill="x", pady=2)
            self.sidebar_buttons[task_key] = btn
        add_btn = ctk.CTkButton(
            scrollable, text="➕ 계정 추가",
            fg_color="#333", height=36, font=self._font_small,
            command=self._on_add_account_click,
        )
        add_btn.pack(fill="x", pady=8)
        self.sidebar_add_btn = add_btn

    def _on_add_account_click(self):
        """계정 추가: 첫 번째 빈 슬롯으로 전환"""
        first_empty = self._get_first_empty_task_key()
        if first_empty:
            self._switch_profile(first_empty)
        else:
            messagebox.showinfo("안내", "모든 슬롯이 사용 중입니다.")

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
        """사이드바 버튼 텍스트를 설정: 연동된 계정이 있으면 '계정 N (아이디)' 형태로 표시"""
        if task_key not in getattr(self, "sidebar_buttons", {}):
            return
        n = self.task_key_to_index.get(task_key, 0)
        label = f"계정 {n}"
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                d = json.load(f).get(task_key)
                if d and d.get("id"):
                    label = f"계정 {n} ({d['id']})"
        except Exception:
            pass
        self.sidebar_buttons[task_key].configure(text=label)

    def _switch_profile(self, task_key):
        """사이드바에서 선택한 프로필(계정)의 화면을 메인 영역에 표시"""
        if self.current_profile and self.current_profile != task_key:
            self.profile_frames[self.current_profile].pack_forget()
        self.profile_frames[task_key].pack(fill="both", expand=True)
        self.current_profile = task_key

    def build_account_detail(self, parent, provider, idx):
        task_key = f"{provider}_{idx}"
        func_tabs = ctk.CTkTabview(parent, segmented_button_fg_color="#333333")
        func_tabs.pack(fill="both", expand=True, padx=2, pady=2)
        t1, t2, t3 = func_tabs.add("⚙ 계정 설정"), func_tabs.add("👥 수신처"), func_tabs.add("📝 메시지 발송")

        setup_box = ctk.CTkFrame(t1, fg_color="transparent")
        setup_box.place(relx=0.5, rely=0.5, anchor="center")
        _e = {"width": 320, "height": 38, "font": self._font_body}
        e_id = ctk.CTkEntry(setup_box, placeholder_text="아이디", **_e); e_id.pack(pady=5)
        e_pw = ctk.CTkEntry(setup_box, placeholder_text="앱 비밀번호", show="*", **_e); e_pw.pack(pady=5)
        e_smtp = ctk.CTkEntry(setup_box, placeholder_text="SMTP 주소", **_e); e_smtp.pack(pady=5)
        e_port = ctk.CTkEntry(setup_box, placeholder_text="포트 (465)", **_e); e_port.pack(pady=5)
        
        try: # 🌟 자동 로드
            with open(self.config_file, 'r', encoding='utf-8') as f:
                d = json.load(f).get(task_key)
                if d: e_id.insert(0, d['id']); e_pw.insert(0, d['pw']); e_smtp.insert(0, d['smtp']); e_port.insert(0, d['port'])
        except: pass

        def verify():
            uid, upw, usmtp, uport = e_id.get().strip(), e_pw.get().strip(), e_smtp.get().strip(), e_port.get().strip()
            def check():
                try:
                    server = smtplib.SMTP_SSL(usmtp, int(uport), timeout=10)
                    server.login(uid, upw); server.quit()
                    with open(self.config_file, 'r+', encoding='utf-8') as f:
                        data = json.load(f); data[task_key] = {"id": uid, "pw": upw, "smtp": usmtp, "port": uport}
                        f.seek(0); json.dump(data, f, indent=4, ensure_ascii=False); f.truncate()
                    self.write_log(provider, idx, "✅ 계정 연동 성공")
                    self.after(0, self._rebuild_sidebar_buttons)
                except Exception as e: self.write_log(provider, idx, f"❌ 실패: {e}")
            threading.Thread(target=check, daemon=True).start()
        ctk.CTkButton(setup_box, text="서버 연결 테스트 및 저장", fg_color="#28a745", command=verify, width=320, height=38, font=self._font_small).pack(pady=12)

        list_f = ctk.CTkFrame(t2, fg_color="transparent")
        list_f.pack(fill="both", expand=True, padx=8, pady=8)
        tree = ttk.Treeview(
            list_f, columns=("no", "comp", "email"), show="headings", height=10,
            selectmode="extended"
        )
        tree.column("no", width=50, anchor="center")
        tree.column("comp", width=180, anchor="w")
        tree.column("email", width=220, anchor="w")
        tree.heading("no", text="No"); tree.heading("comp", text="업체명"); tree.heading("email", text="이메일")
        self.tree_views[task_key] = tree
        tree.pack(fill="both", expand=True, pady=(0, 6))

        count_lbl = ctk.CTkLabel(list_f, text="등록된 수신처 없음", font=self._font_small, text_color="#bdc3c7")
        count_lbl.pack(anchor="w", pady=(0, 6))

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
            path = filedialog.askopenfilename(filetypes=[("Excel Files", "*.xlsx *.xls *.csv")])
            if not path:
                return

            try:
                import pandas as pd
            except ImportError:
                messagebox.showerror("패키지 없음", "pandas가 설치되어 있지 않아 엑셀 파일을 불러올 수 없습니다.\n\npip install pandas")
                return

            try:
                df = pd.read_csv(path) if path.endswith('.csv') else pd.read_excel(path)
            except Exception as e:
                messagebox.showerror("불러오기 실패", f"엑셀 파일을 읽는 중 오류가 발생했습니다:\n{e}")
                return

            headers = list(df.columns)
            start = len(tree.get_children()) + 1
            rows = []
            for i, (_, r) in enumerate(df.iterrows(), start=start):
                comp = r.get('업체명', '') if '업체명' in r else (str(r.iloc[0]) if len(r) > 0 else '')
                email = r.get('이메일', '') if '이메일' in r else (str(r.iloc[1]) if len(r) > 1 else '')
                tree.insert("", "end", values=(i, comp, email))
                
                # 모든 엑셀 데이터 저장 (동적 변수용)
                row_data = {}
                for col in df.columns:
                    val = r.get(col, '')
                    row_data[col] = str(val) if val is not None and str(val).strip() else ''
                rows.append(row_data)
            
            update_count_label()
            self.save_recipients_rows(task_key, rows, headers)

        def clear_excel():
            for item in tree.get_children():
                tree.delete(item)
            update_count_label()
            self.save_recipients_rows(task_key, [], [])

        def delete_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("안내", "삭제할 행을 선택해 주세요.", parent=t2)
                return
            # Task 2-1: 트리 행 순서와 recipients.json의 rows[] 인덱스가 1:1이므로, 삭제 시 동일 인덱스를 pop
            state = self.load_recipients_state(task_key)
            rows = list(state.get("rows", []))
            headers = state.get("headers", [])
            children = list(tree.get_children())
            sel_set = set(sel)
            indices_to_remove = [i for i, iid in enumerate(children) if iid in sel_set]
            for idx in sorted(indices_to_remove, reverse=True):
                if 0 <= idx < len(rows):
                    rows.pop(idx)
            for iid in sel:
                tree.delete(iid)
            refresh_row_numbers()
            update_count_label()
            # 인덱스 불일치 시(과거 데이터 등): 트리 표시 순서로 rows 재구성
            tree_ids = list(tree.get_children())
            if len(rows) != len(tree_ids):
                rows = []
                for iid in tree_ids:
                    v = tree.item(iid)["values"]
                    comp = v[1] if len(v) > 1 else ""
                    email = v[2] if len(v) > 2 else ""
                    rows.append({"업체명": comp, "이메일": email})
            self.save_recipients_rows(task_key, rows, headers)

        btn_row = ctk.CTkFrame(list_f, fg_color="transparent")
        btn_row.pack(pady=4)
        ctk.CTkButton(btn_row, text="📁 엑셀 추가하기", command=load_excel, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="🧹 전체 초기화", fg_color="#7f8c8d", command=clear_excel, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="🗑 선택 행 삭제", fg_color="#c0392b", command=delete_selected, font=self._font_small).pack(side="left", padx=4)
        ctk.CTkLabel(btn_row, text="(Ctrl·Shift+클릭으로 여러 행 선택)", font=("맑은 고딕", 10), text_color="#7f8c8d").pack(side="left", padx=8)

        export_btn_row = ctk.CTkFrame(list_f, fg_color="transparent")
        export_btn_row.pack(pady=4)
        ctk.CTkButton(export_btn_row, text="📊 발송 결과 엑셀로 저장", fg_color="#9b59b6", command=lambda: self._export_to_excel(task_key), font=self._font_small).pack(side="left", padx=4)
        ctk.CTkButton(export_btn_row, text="🚫 수신 거부 목록 관리", fg_color="#e74c3c", command=self._open_blacklist_manager, font=self._font_small).pack(side="left", padx=4)

        # 재실행 후에도 수신처 자동 복원
        state = self.load_recipients_state(task_key)
        rows = state.get("rows", [])
        if isinstance(rows, list) and rows:
            start = len(tree.get_children()) + 1
            for i, r in enumerate(rows, start=start):
                if isinstance(r, dict):
                    comp = r.get("업체명") or r.get("comp", "")
                    email = r.get("이메일") or r.get("email", "")
                    tree.insert("", "end", values=(i, comp, email))
                else:
                    try:
                        tree.insert("", "end", values=(i, r[0], r[1]))
                    except Exception:
                        pass
            update_count_label()

        send_f = ctk.CTkFrame(t3, fg_color="transparent")
        send_f.pack(fill="both", expand=True, padx=12, pady=8)
        cur_d = {"files": [], "imgs": {}}
        toolbar = ctk.CTkFrame(send_f, fg_color="transparent")
        toolbar.pack(fill="x", pady=4)
        ctk.CTkButton(toolbar, text="📂 템플릿", width=90, height=32, font=self._font_small, command=lambda: self.open_tpl_library(title_e, body_t, sender_e, cur_d, f_lbl, i_lbl)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="✍️ 에디터", width=90, height=32, fg_color="#2980b9", font=self._font_small, command=lambda: self._open_editor_for_body(body_t)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="💾 저장", fg_color="#28a745", width=80, height=32, font=self._font_small, command=lambda: self.save_tpl(title_e, body_t, sender_e, cur_d, task_key)).pack(side="left", padx=3)
        ctk.CTkButton(toolbar, text="📎 파일", width=80, height=32, fg_color="#555", font=self._font_small, command=lambda: self.attach_file(cur_d, f_lbl)).pack(side="right", padx=3)
        ctk.CTkButton(toolbar, text="🖼️ CID", width=80, height=32, fg_color="#555", font=self._font_small, command=lambda: self.attach_cid(cur_d, i_lbl)).pack(side="right", padx=3)

        title_e = ctk.CTkEntry(send_f, placeholder_text="제목 {업체명}", height=38, font=self._font_body); title_e.pack(fill="x", pady=4)
        sender_e = ctk.CTkEntry(send_f, placeholder_text="보내는 사람 이름", height=38, border_color="#1F6AA5", font=self._font_body); sender_e.pack(fill="x", pady=4)
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
        ctk.CTkLabel(
            interval_f,
            text="중복 발송 차단: 같은 템플릿+같은 수신처는 1회만 발송됩니다.",
            font=("맑은 고딕", 11),
        ).pack(side="left", padx=12)

        def start():
            self.stop_flags[task_key] = False
            interval = interval_cb.get()
            prevent_dup = prevent_dup_var.get()

            # 1계정당 1템플릿 규칙: 템플릿 라이브러리명 우선, 없으면 제목 (Task 2-2: real_engine과 동일 키)
            template_name = _dedup_template_key(self.current_template_name.get(task_key), title_e.get())
            self.current_template_name[task_key] = template_name

            threading.Thread(
                target=self.real_engine,
                args=(
                    provider,
                    idx,
                    title_e.get(),
                    body_t.get("1.0", "end-1c"),
                    sender_e.get(),
                    cur_d,
                    interval,
                    prevent_dup,
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
        test_b = ctk.CTkButton(btn_f, text="🧪 테스트 발송", height=42, fg_color="#3498db", font=self._font_small, command=lambda: self._start_test_send(provider, idx, title_e.get(), body_t.get("1.0", "end-1c"), sender_e.get(), cur_d, send_b, test_b))
        test_b.pack(side="left", fill="x", expand=True, padx=4)
        stop_b = ctk.CTkButton(btn_f, text="🛑 중지", height=42, state="disabled", font=self._font_small, command=lambda: self.set_stop(task_key))
        stop_b.pack(side="right", fill="x", expand=True, padx=4)

        f_lbl = ctk.CTkLabel(send_f, text="📎 첨부: 없음", text_color="#95a5a6", font=self._font_small); f_lbl.pack(anchor="w", pady=2)
        i_lbl = ctk.CTkLabel(send_f, text="🖼️ CID: 없음", text_color="#95a5a6", font=self._font_small); i_lbl.pack(anchor="w", pady=2)
        log_t = ctk.CTkTextbox(send_f, height=72, font=("Consolas", 11), fg_color="#1e1e1e", text_color="#00ff00")
        log_t.pack(fill="x", pady=4)
        log_t.configure(state="disabled"); self.log_consoles[task_key] = log_t

        progress_lbl = ctk.CTkLabel(send_f, text="마지막 성공 전송: 없음", font=self._font_small, text_color="#bdc3c7")
        progress_lbl.pack(anchor="w", pady=(2, 0))
        self.progress_labels[task_key] = progress_lbl

        # 재실행 후 마지막 성공 전송 정보 복원
        try:
            last_sent = self.load_recipients_state(task_key).get("last_sent", {}) or {}
            if last_sent:
                no = last_sent.get("no", "?")
                comp = last_sent.get("comp", "")
                email = last_sent.get("email", "")
                at = last_sent.get("at", "")
                progress_lbl.configure(text=f"마지막 성공 전송: {no}행  {comp} <{email}>  ({at})")
        except Exception:
            pass

        # (start/버튼은 위에서 이미 배치됨)

    def _apply_dynamic_variables(self, text, row_data):
        """엑셀 데이터를 이용해 동적 변수 치환 ({변수명} → 값)"""
        result = text
        for key, value in row_data.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value or ""))
        return result

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

    def real_engine(self, p, i, title, body, s_name, data, interval, prevent_dup, tree, s_b, st_b, template_name=""): #
        key = f"{p}_{i}"; self.after(0, lambda: (s_b.configure(state="disabled"), st_b.configure(state="normal", fg_color="#dc3545")))
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f: config = json.load(f).get(key)
            
            # 트리뷰에서 표시 정보만 추출
            tree_values = [tree.item(item)['values'] for item in tree.get_children()]
            
            # recipients.json에서 모든 데이터 로드 (동적 변수용)
            state = self.load_recipients_state(key)
            all_rows = state.get("rows", [])
            
            if not config or not all_rows: 
                self.write_log(p, i, "❌ 계정/수신처 부족")
                return

            # 1계정당 1템플릿 원칙 (Task 2-2: start()와 동일한 _dedup_template_key로 actual_template 고정)
            actual_template = _dedup_template_key(template_name, title)
            con = sqlite3.connect(self.db_path)
            try:
                cur = con.execute(
                    "SELECT 1 FROM sent_log WHERE task_key=? AND IFNULL(TRIM(template_name), '')<>? LIMIT 1",
                    (key, actual_template),
                )
                if cur.fetchone():
                    self.write_log(
                        p,
                        i,
                        f"❌ 이미 다른 템플릿으로 발송된 기록이 있습니다. 동일 템플릿만 사용할 수 있습니다. [현재 템플릿 키: 「{actual_template}」]",
                    )
                    return
            finally:
                con.close()

            # --- Task 1-1 Pre-Compose: 서버 접속 전에 모든 MIME 조립 ---
            pre_composed = []
            for idx, row_data in enumerate(all_rows, 1):
                if self.stop_flags[key]:
                    break
                comp = row_data.get("업체명") or row_data.get("comp", "")
                email = row_data.get("이메일") or row_data.get("email", "")
                no = idx
                try:
                    if prevent_dup:
                        dup, dup_reason = self.check_duplicate_send_status(email, actual_template)
                        if dup:
                            if dup_reason == "other_sender":
                                self.write_log(
                                    p,
                                    i,
                                    f"🚫 [{idx}/{len(all_rows)}] {comp} <{email}> 스킵 — [사유: 타 담당자 발송 이력] 시도 템플릿: 「{actual_template}」",
                                )
                            else:
                                self.write_log(
                                    p,
                                    i,
                                    f"⏭️ [{idx}/{len(all_rows)}] {comp} <{email}> 스킵 — [사유: 동일 템플릿 재발송] 템플릿: 「{actual_template}」",
                                )
                            continue
                    if self._is_blacklisted(email):
                        self.write_log(p, i, f"🚫 [{idx}/{len(all_rows)}] {comp} <{email}> 스킵(수신 거부 업체)")
                        continue
                    final_title = self._apply_dynamic_variables(title, row_data)
                    final_body = self._apply_dynamic_variables(body, row_data)
                    msg = self._build_single_mime(config, s_name, email, final_title, final_body, data, comp)
                    pre_composed.append((msg, idx, comp, email, final_title, no))
                except Exception as e:
                    self.write_log(p, i, f"❌ [{idx}/{len(all_rows)}] {comp} <{email}> MIME 조립 오류: {e}")

            # --- Task 1-2 Just-In-Time: 조립된 메일별로 접속 → 발송 → 종료 ---
            total = len(pre_composed)
            for pos, (msg, idx, comp, email, final_title, no) in enumerate(pre_composed):
                if self.stop_flags[key]:
                    break
                try:
                    success, msg_text = self._send_with_retry(config, msg, max_retries=3)
                    if success:
                        self.write_log(p, i, f"✅ [{idx}/{len(all_rows)}] {comp} <{email}> 성공")
                        self.update_last_sent_state(key, no, comp, email)
                        eff_tpl = self._effective_template_for_log(actual_template, final_title)
                        self.record_success_to_db(key, p, i, comp, email, final_title, actual_template)
                        self.after(0, lambda c=comp, em=email, t=eff_tpl: self._append_cloud_sent_row(c, em, t))
                        self._update_stats_label()
                        lbl = self.progress_labels.get(key)
                        if lbl:
                            at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            self.after(0, lambda n=no, c=comp, e=email, a=at: lbl.configure(text=f"마지막 성공 전송: {n}행  {c} <{e}>  ({a})"))
                    else:
                        self.write_log(p, i, f"❌ [{idx}/{len(all_rows)}] {comp} <{email}> 재시도 실패: {self._translate_smtp_error(msg_text)}")
                except Exception as e:
                    self.write_log(p, i, f"❌ [{idx}/{len(all_rows)}] {comp} <{email}> 발송 오류: {self._translate_smtp_error(str(e))}")
                if pos < total - 1:
                    wait_sec = self.get_wait_seconds(interval)
                    for _ in range(wait_sec):
                        if self.stop_flags[key]:
                            break
                        time.sleep(1)
            self.write_log(p, i, "🏁 모든 작업 종료")
        finally: self.reset_btns(s_b, st_b)

    def write_log(self, p, i, m):
        key = f"{p}_{i}"; box = self.log_consoles[key]; box.configure(state="normal")
        box.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {m}\n"); box.see("end"); box.configure(state="disabled")

    def set_stop(self, k): self.stop_flags[k] = True
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
                "SELECT id, provider, account_idx as account, comp, email, subject, template_name, sent_at FROM sent_log WHERE task_key=? ORDER BY sent_at DESC",
                con,
                params=(task_key,)
            )
        finally:
            con.close()

        if df.empty:
            messagebox.showinfo("알림", f"발송 결과가 없습니다.")
            return

        # 엑셀 파일 저장 경로 선택
        file_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx"), ("All Files", "*.*")],
            initialfile=f"{task_key}_발송결과_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
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
        BlacklistManager(self, self)

    def _sync_blacklist_from_sheet(self):
        """Phase 5 Task 5-1: 구글 시트 'blacklist' 워크시트에서 읽어 로컬 blacklist 테이블 최신화.
        시트 1행=공지, 2행=헤더, 3행부터 데이터. B열(인덱스1)=업체명, C열(인덱스2)=이메일.
        반환: (성공 여부, 성공 시 동기화 건수 / 실패 시 오류 메시지)"""
        if gspread is None:
            return False, "gspread가 설치되어 있지 않습니다. pip install gspread google-auth"
        cred_path = os.path.join(BASE_DIR, "credentials.json")
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

    def _build_single_mime(self, config, s_name, to_email, final_title, final_body, data, comp):
        """Task 1-1: 서버 접속 없이 MIME 메시지 1통만 조립. (Pre-Compose)"""
        msg = MIMEMultipart()
        msg['From'] = formataddr((str(Header(s_name or "운영사무국", 'utf-8')), config['id']))
        msg['To'] = to_email
        msg['Subject'] = Header(final_title, 'utf-8')
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
        to_email = simpledialog.askstring("테스트 메일", "테스트 수신처 이메일:", parent=self)
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

                msg = MIMEMultipart()
                msg['From'] = formataddr((str(Header(sender_name or "운영사무국", 'utf-8')), config['id']))
                msg['To'] = to_email
                msg['Subject'] = Header(title, 'utf-8')

                body_html, embedded_imgs = self._process_body_html(body, "테스트")
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

    def attach_file(self, d, l):
        ps = filedialog.askopenfilenames(); 
        if ps: d["files"] = list(ps); l.configure(text=f"📎 첨부: {len(d['files'])}개")
    def attach_cid(self, d, l):
        p = filedialog.askopenfilename(); 
        if p:
            c = simpledialog.askstring("CID", "CID 이름:"); 
            if c: d["imgs"][c] = p; l.configure(text=f"🖼️ CID: {len(d['imgs'])}개")

    def save_tpl(self, t, b, s, d, task_key=None):
        n = simpledialog.askstring("저장", "템플릿 이름:"); 
        if n:
            with open(self.template_file, 'r+', encoding='utf-8') as f:
                data = json.load(f); data[n] = {"title": t.get(), "body": b.get("1.0", "end-1c"), "sender": s.get(), "files": d["files"], "imgs": d["imgs"]}
                f.seek(0); json.dump(data, f, indent=4, ensure_ascii=False); f.truncate()
            messagebox.showinfo("완료", f"'{n}' 저장 성공")
            if task_key:
                self.current_template_name[task_key] = n

    def open_tpl_library(self, t, b, s, d, f_l, i_l):
        pop = ctk.CTkToplevel(self); pop.title("템플릿"); pop.geometry("300x400"); pop.attributes("-topmost", True)
        frame = ctk.CTkScrollableFrame(pop); frame.pack(fill="both", expand=True, padx=5, pady=5)
        with open(self.template_file, 'r', encoding='utf-8') as f:
            tpls = json.load(f)
            for name in tpls.keys():
                row = ctk.CTkFrame(frame, fg_color="transparent"); row.pack(fill="x", pady=1)
                def apply(n=name):
                    with open(self.template_file, 'r', encoding='utf-8') as f:
                        tpl = json.load(f).get(n)
                        t.delete(0, 'end'); t.insert(0, tpl['title'])
                        b.delete("1.0", "end"); b.insert("1.0", tpl['body'])
                        s.delete(0, 'end'); s.insert(0, tpl.get('sender', ''))
                        d["files"], d["imgs"] = tpl.get('files', []), tpl.get('imgs', {})
                        f_l.configure(text=f"📎 첨부: {len(d['files'])}개"); i_l.configure(text=f"🖼️ CID: {len(d['imgs'])}개")
                        self.current_template_name[task_key] = n
                    pop.destroy()
                ctk.CTkButton(row, text=name, command=apply, width=180).pack(side="left", expand=True, fill="x", padx=1)
                ctk.CTkButton(row, text="X", width=25, fg_color="red", command=lambda n=name: self.del_tpl(n, pop)).pack(side="right")

    def del_tpl(self, n, p):
        if messagebox.askyesno("삭제", f"'{n}' 삭제할까요?", parent=p):
            with open(self.template_file, 'r+', encoding='utf-8') as f:
                data = json.load(f); del data[n]; f.seek(0); json.dump(data, f, indent=4, ensure_ascii=False); f.truncate()
            p.destroy()