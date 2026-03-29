import customtkinter as ctk
import gspread, hashlib, json, os, re, subprocess, sys, time, unicodedata, webbrowser
from datetime import datetime
from tkinter import messagebox
import threading
import uuid
try:
    import requests
except ImportError:
    requests = None

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Phase 6 Task 6-1: 구글 시트 버전과 비교할 앱 현재 버전
CURRENT_VERSION = "v2.6.8"
SPREADSHEET_KEY = "1I5cdNtpJYQuzYt0juhOcgbcltTv7wb3BJFI2AnI2Crw"

# GitHub 릴리스 연동: 시트 B1이 비어 있거나 "GITHUB"이면 최신 Release의 .exe URL 사용 (구글 드라이브 불필요)
# 우선순위: 환경변수 MAILMONSTER_GITHUB_REPO > BASE_DIR/github_release_repo.txt > 아래 기본값
# 끄려면: 환경변수 MAILMONSTER_DISABLE_GITHUB_RELEASE=1
GITHUB_RELEASE_REPO_DEFAULT = "Sanghee-Park/mail-monster-pro"


def _strip_invisible_chars(s):
    """시트/복사 시 끼는 zero-width, BOM, NBSP 등 제거 (strip()만으로는 안 지워지는 경우 있음)."""
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


def _normalize_version_for_compare(s):
    """Task 1-1: 시트/앱 버전 문자열을 strip().lower() 적용 후 비교 (공백·대소문자 오류 방지)."""
    if s is None:
        return ""
    t = _strip_invisible_chars(s).lower()
    t = t.replace("\t", " ").replace("\r", "").replace("\n", " ")
    t = t.strip()
    if len(t) > 1 and t.startswith("v") and (t[1].isdigit() or t[1] == "."):
        t = t[1:].lstrip()
    return t.strip()


def _version_numeric_tuple(s):
    """'2.5.2', 'v2.5.2', 시트 숫자 셀 등에서 숫자 세그먼트 튜플 추출 (예: (2, 5, 2))."""
    t = _normalize_version_for_compare(s)
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


def _versions_effectively_equal(sheet_version, app_version):
    """시트 값과 앱 CURRENT_VERSION이 같은 버전이면 True (공백·유니코드·v 접두·숫자 형식 차이 허용)."""
    ta = _version_numeric_tuple(sheet_version)
    tb = _version_numeric_tuple(app_version)
    if ta and tb and ta == tb:
        return True
    a = _normalize_version_for_compare(sheet_version)
    b = _normalize_version_for_compare(app_version)
    return bool(a) and bool(b) and a == b


def _resolve_github_release_repo():
    """GitHub owner/repo 문자열 (릴리스 API용)."""
    if os.environ.get("MAILMONSTER_DISABLE_GITHUB_RELEASE", "").strip().lower() in ("1", "true", "yes"):
        return ""
    if "MAILMONSTER_GITHUB_REPO" in os.environ:
        env = os.environ.get("MAILMONSTER_GITHUB_REPO", "").strip()
        if env and "/" in env:
            return env
        return ""
    path = os.path.join(BASE_DIR, "github_release_repo.txt")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "/" in line:
                        return line
        except Exception:
            pass
    return GITHUB_RELEASE_REPO_DEFAULT


def _github_latest_release_meta():
    """GitHub API: 최신 Release — (tag, exe_url, sha256_hex|None, release_html_url).
    sha256는 에셋 MAIL_MONSTER_PRO.exe.sha256 내용에서 추출(릴리스 워크플로에서 첨부)."""
    if requests is None:
        return "", "", None, ""
    repo = _resolve_github_release_repo()
    if not repo or "/" not in repo:
        return "", "", None, ""
    headers = {"Accept": "application/vnd.github+json"}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=25,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        tag = (data.get("tag_name") or "").strip()
        html_url = (data.get("html_url") or "").strip()
        assets = data.get("assets") or []
        sha_hex = None
        for a in assets:
            n = (a.get("name") or "").upper()
            if n == "MAIL_MONSTER_PRO.EXE.SHA256":
                u = (a.get("browser_download_url") or "").strip()
                if u:
                    try:
                        ru = requests.get(u, timeout=25, headers=headers)
                        ru.raise_for_status()
                        m = re.search(r"[a-fA-F0-9]{64}", ru.text or "")
                        if m:
                            sha_hex = m.group(0).lower()
                    except Exception:
                        pass
                break
        preferred, rest = [], []
        for a in assets:
            n = a.get("name") or ""
            if not n.lower().endswith(".exe"):
                continue
            if n.upper() == "MAIL_MONSTER_PRO.EXE":
                preferred.append(a)
            else:
                rest.append(a)
        exe_url = ""
        for a in preferred + rest:
            u = (a.get("browser_download_url") or "").strip()
            if u:
                exe_url = u
                break
        return tag, exe_url, sha_hex, html_url
    except Exception:
        return "", "", None, ""


def _file_sha256_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _unblock_downloaded_file_win(path):
    """인터넷에서 받은 파일의 Zone.Identifier 제거(무료). 일부 '차단 해제' 경고 완화에 도움."""
    if sys.platform != "win32" or not path or not os.path.isfile(path):
        return
    try:
        flags = subprocess.CREATE_NO_WINDOW if (sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")) else 0
        ps_path = path.replace("'", "''")
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Unblock-File -LiteralPath '{ps_path}'",
            ],
            timeout=45,
            capture_output=True,
            creationflags=flags,
            check=False,
        )
    except Exception:
        pass


class LoginApp(ctk.CTk):
    def __init__(self, on_success):
        super().__init__()
        self.on_success = on_success
        self.settings_file = os.path.join(BASE_DIR, "login_settings.json")
        self.update_required = False
        self.update_url = ""
        self.latest_version = ""
        self.update_sha256_expected = None
        self.update_release_page_url = ""
        self.title(f"메일 몬스터 - 보안 로그인 ({CURRENT_VERSION})")
        self.geometry("400x650")
        ctk.set_appearance_mode("dark")
        try: self.iconbitmap(os.path.join(BASE_DIR, "pro.ico"))
        except: pass
        self.setup_login_ui()
        self.load_settings()
        # 업데이트 체크는 앱 시작 시점이 아니라 "로그인 성공 직후"에 수행한다.

    def get_mac_address(self):
        return hex(uuid.getnode())

    def _check_update_from_sheet(self):
        """Phase 6 Task 6-1: 구글 시트 worksheet('설정') A1=최신 버전명, B1=exe 다운로드 링크를 읽어 업데이트 필요 여부 판별"""
        try:
            cred_path = os.path.join(BASE_DIR, "credentials.json")
            if not os.path.exists(cred_path):
                return
            client = gspread.service_account(filename=cred_path)
            spreadsheet = client.open_by_key(SPREADSHEET_KEY)
            ws = spreadsheet.worksheet("설정")
            a1_raw = ws.acell("A1").value
            b1_raw = ws.acell("B1").value
            a1 = str(a1_raw or "").strip()
            b1 = str(b1_raw or "").strip()
            # B1에 URL이 없으면 강제 업데이트 없음
            if not b1:
                return
            # A1 비어 있으면 버전 비교 불가 → 업데이트 유도하지 않음 (로그인만)
            if not a1:
                return
            # 시트 버전과 앱 버전이 같으면 업데이트 없이 로그인 화면 유지
            if _versions_effectively_equal(a1, CURRENT_VERSION):
                return
            # 메인 스레드에서 한 번 더 검증 후 팝업 (시트·앱 문자열 차이 방지)
            self.after(0, lambda av=a1, url=b1: self._begin_update_if_needed(av, url))
        except Exception:
            pass

    def _begin_update_if_needed(self, latest_version, update_url):
        """UI 스레드: 버전이 여전히 다를 때만 업데이트 진행. 같으면 창 숨김 없이 그대로 로그인 가능."""
        if _versions_effectively_equal(latest_version, CURRENT_VERSION):
            return
        if not (update_url or "").strip():
            return
        self._set_update_required(latest_version, update_url)

    def _set_update_required(self, latest_version, update_url, sha256_expected=None, release_page_url=""):
        """업데이트 필요 상태 설정 후 팝업 표시 및 다운로드 시작 (Task 6-2)"""
        self.update_required = True
        self.latest_version = latest_version
        self.update_url = update_url
        self.update_sha256_expected = sha256_expected
        self.update_release_page_url = (release_page_url or "").strip()
        self.withdraw()
        self._show_update_popup_and_download()

    def _show_update_popup_and_download(self):
        """Task 6-2: 필수 업데이트 팝업 + CTkProgressBar + 백그라운드 다운로드(청크 단위 진행률)"""
        if requests is None:
            messagebox.showerror("오류", "업데이트를 위해 requests 모듈이 필요합니다.\npip install requests")
            self.deiconify()
            return
        pop = ctk.CTkToplevel(self)
        pop.title("필수 업데이트")
        pop.geometry("400x200")
        pop.attributes("-topmost", True)
        ctk.CTkLabel(pop, text="필수 업데이트 진행 중...", font=("맑은 고딕", 14, "bold")).pack(pady=(24, 12))
        prog = ctk.CTkProgressBar(pop, width=320, height=16)
        prog.pack(pady=12, padx=40)
        prog.set(0)
        status_lbl = ctk.CTkLabel(pop, text="0%", font=("맑은 고딕", 11), text_color="#95a5a6")
        status_lbl.pack(pady=4)
        new_exe_path = os.path.join(BASE_DIR, "MailMonster_new.exe")

        def _to_direct_download_url(url):
            """구글 드라이브 공유 링크(드라이브 URL)를 직접 다운로드 URL로 변환"""
            s = (url or "").strip()
            if "drive.google.com" in s and "/file/d/" in s:
                import re
                m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", s)
                if m:
                    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
            return s

        download_url = _to_direct_download_url(self.update_url)

        def download_worker():
            last_err = None
            headers = {"User-Agent": "MailMonster-Updater/1.0"}
            for attempt in range(3):
                try:
                    r = requests.get(download_url, stream=True, timeout=300, headers=headers)
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0)) or None
                    downloaded = 0
                    chunk_count = 0
                    with open(new_exe_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                chunk_count += 1
                                if total and total > 0:
                                    pct = min(1.0, downloaded / total)
                                    self.after(0, lambda v=pct: prog.set(v))
                                    self.after(0, lambda v=downloaded, t=total: status_lbl.configure(text=f"{int(v*100//t)}%"))
                                else:
                                    pct = min(0.95, chunk_count * 0.002)
                                    self.after(0, lambda v=pct: prog.set(v))
                                    self.after(0, lambda d=downloaded: status_lbl.configure(text=f"다운로드 중... {d:,} bytes"))
                    self.after(0, lambda: self._on_update_download_complete(pop, new_exe_path))
                    return
                except Exception as e:
                    last_err = e
                    try:
                        if os.path.isfile(new_exe_path):
                            os.remove(new_exe_path)
                    except Exception:
                        pass
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            self.after(0, lambda err=str(last_err): self._on_update_download_failed(pop, err))

        threading.Thread(target=download_worker, daemon=True).start()

    def _on_update_download_complete(self, pop, new_exe_path):
        """Task 6-3: 다운로드 완료 → 무결성 검사 → 차단 해제 → BAT로 교체 후 종료"""
        pop.destroy()
        if not os.path.exists(new_exe_path):
            messagebox.showerror("업데이트 실패", "다운로드된 파일을 찾을 수 없습니다.")
            self.deiconify()
            return
        exp = self.update_sha256_expected
        if exp:
            try:
                got = _file_sha256_hex(new_exe_path)
                if got != exp.lower():
                    try:
                        os.remove(new_exe_path)
                    except Exception:
                        pass
                    messagebox.showerror(
                        "업데이트 실패",
                        "다운로드 파일이 손상되었거나 변조되었을 수 있습니다.\n(SHA256 불일치)\n브라우저에서 릴리스 페이지를 열어 수동 설치해 주세요.",
                    )
                    page = (self.update_release_page_url or self.update_url or "").strip()
                    if page:
                        try:
                            webbrowser.open(page)
                        except Exception:
                            pass
                    self.deiconify()
                    return
            except Exception as e:
                messagebox.showerror("업데이트 실패", f"파일 검증 오류: {e}")
                self.deiconify()
                return
        _unblock_downloaded_file_win(new_exe_path)
        exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("업데이트 완료", "다운로드가 완료되었습니다.\n실행 파일로 빌드된 환경에서만 자동 교체가 적용됩니다.")
            self.deiconify()
            return
        ps_path = os.path.join(BASE_DIR, "_update_runner.ps1")
        exe_ps = exe_path.replace("'", "''")
        new_ps = new_exe_path.replace("'", "''")
        script_ps = ps_path.replace("'", "''")
        ps_content = f"""$ErrorActionPreference = 'SilentlyContinue'
Start-Sleep -Seconds 2
for ($i=0; $i -lt 20; $i++) {{
    try {{
        Remove-Item -LiteralPath '{exe_ps}' -Force -ErrorAction Stop
        break
    }} catch {{
        Start-Sleep -Milliseconds 500
    }}
}}
Move-Item -LiteralPath '{new_ps}' -Destination '{exe_ps}' -Force
Start-Process -FilePath '{exe_ps}'
Start-Sleep -Milliseconds 500
Remove-Item -LiteralPath '{script_ps}' -Force
"""
        try:
            with open(ps_path, "w", encoding="utf-8") as f:
                f.write(ps_content)
            flags = subprocess.CREATE_NO_WINDOW if (sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")) else 0
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    ps_path,
                ],
                creationflags=flags,
                cwd=BASE_DIR,
            )
            os._exit(0)
        except Exception as e:
            messagebox.showerror("업데이트 실패", f"배치 실행 오류: {e}")
            self.deiconify()

    def _on_update_download_failed(self, pop, err_msg):
        pop.destroy()
        page = (self.update_release_page_url or "").strip()
        url = (self.update_url or "").strip()
        open_url = page or url
        if open_url:
            try:
                webbrowser.open(open_url)
            except Exception:
                pass
            messagebox.showerror(
                "업데이트 실패",
                "자동 다운로드에 실패했습니다. 브라우저에서 릴리스/다운로드 페이지를 열었습니다.\n"
                "Windows가 실행을 막으면 '추가 정보' → '실행'을 눌러 주세요.\n\n"
                f"{err_msg}",
            )
        else:
            messagebox.showerror("업데이트 실패", f"다운로드 중 오류가 발생했습니다.\n{err_msg}")
        self.deiconify()

    def setup_login_ui(self):
        ctk.CTkLabel(self, text="MAIL MONSTER", font=("Impact", 35)).pack(pady=(40, 4))
        ctk.CTkLabel(self, text=CURRENT_VERSION, font=("맑은 고딕", 12), text_color="#7f8c8d").pack(pady=(0, 16))
        self.id_ent = ctk.CTkEntry(self, placeholder_text="아이디", width=280, height=45); self.id_ent.pack(pady=10)
        self.pw_ent = ctk.CTkEntry(self, placeholder_text="비밀번호", show="*", width=280, height=45); self.pw_ent.pack(pady=10)
        
        cb_f = ctk.CTkFrame(self, fg_color="transparent"); cb_f.pack(pady=10)
        self.save_id_var = ctk.BooleanVar(); ctk.CTkCheckBox(cb_f, text="ID 저장", variable=self.save_id_var).pack(side="left", padx=5)
        self.auto_login_var = ctk.BooleanVar(); ctk.CTkCheckBox(cb_f, text="자동 로그인", variable=self.auto_login_var).pack(side="left", padx=5)

        ctk.CTkButton(self, text="로그인", width=280, height=45, command=self.check_login).pack(pady=20)
        ctk.CTkButton(self, text="회원가입 신청", width=280, height=40, fg_color="transparent", border_width=1, command=self.open_reg).pack()

    def load_settings(self):
        if os.path.exists(self.settings_file):
            with open(self.settings_file, "r", encoding='utf-8') as f:
                d = json.load(f)
                if d.get("save_id"): self.id_ent.insert(0, d.get("id", "")); self.save_id_var.set(True)
                if d.get("auto_login"): self.pw_ent.insert(0, d.get("pw", "")); self.auto_login_var.set(True); self.after(500, self.check_login)

    def _fetch_update_info(self):
        """설정 시트 A1=최신 버전, B1=다운로드 URL.
        B1이 비어 있거나 'GITHUB'/'GIT'(대소문자 무시)이면 GitHub 최신 Release의 exe URL 사용 (기본 저장소: GITHUB_RELEASE_REPO_DEFAULT).
        B1에 직접 URL을 넣으면 그대로 사용(수동 덮어쓰기).
        반환: (latest, url, sha256_hex|None, release_page_url) — GitHub 자동일 때만 sha256·릴리스 페이지 채움."""
        latest, url = "", ""
        sha256_hex, release_page = None, ""
        try:
            cred_path = os.path.join(BASE_DIR, "credentials.json")
            if os.path.exists(cred_path):
                client = gspread.service_account(filename=cred_path)
                spreadsheet = client.open_by_key(SPREADSHEET_KEY)
                ws = spreadsheet.worksheet("설정")
                v = ws.acell("A1").value
                u = ws.acell("B1").value
                latest = str(v).strip() if v is not None else ""
                raw_b = str(u).strip() if u is not None else ""
                if raw_b.lower() in ("", "github", "git"):
                    url = ""
                else:
                    url = raw_b
        except Exception:
            pass
        if not url:
            gh_tag, gh_url, gh_sha, gh_page = _github_latest_release_meta()
            if gh_url:
                url = gh_url
                sha256_hex = gh_sha
                release_page = gh_page
                if not latest and gh_tag:
                    latest = gh_tag
        return latest, url, sha256_hex, release_page

    def _launch_main_app(self, user_name, grade, rem):
        self.withdraw()
        self.quit()
        self.on_success(user_name, grade, rem)

    def _check_update_after_login(self, user_name, grade, rem):
        """로그인 성공 직후 버전 확인: 같으면 실행, 다르면 권고 후 업데이트."""
        latest_version, update_url, sha256_exp, release_page = self._fetch_update_info()
        # A1에 버전이 있고 앱과 동일하면 B1(URL) 유무와 관계없이 즉시 실행 (같은 버전인데도 권고 팝업 방지)
        if latest_version and _versions_effectively_equal(latest_version, CURRENT_VERSION):
            self._launch_main_app(user_name, grade, rem)
            return
        if (not latest_version) or (not update_url):
            self._launch_main_app(user_name, grade, rem)
            return

        msg = (
            f"새 버전이 있습니다.\n"
            f"현재: {CURRENT_VERSION}\n"
            f"최신: {latest_version}\n\n"
            f"지금 업데이트를 진행할까요?"
        )
        do_update = messagebox.askyesno("업데이트 권고", msg)
        if do_update:
            self._set_update_required(latest_version, update_url, sha256_exp, release_page)
        else:
            self._launch_main_app(user_name, grade, rem)

    def check_login(self):
        uid, upw = self.id_ent.get().strip(), self.pw_ent.get().strip()
        my_mac = self.get_mac_address()
        try:
            client = gspread.service_account(filename=os.path.join(BASE_DIR, 'credentials.json'))
            sheet = client.open_by_key("1I5cdNtpJYQuzYt0juhOcgbcltTv7wb3BJFI2AnI2Crw").sheet1 #
            all_data = sheet.get_all_values()
            for i, row in enumerate(all_data[1:], start=2):
                if row[0] == uid and row[1] == upw:
                    user_name, grade, raw_period = row[2], row[4], row[5]
                    while len(row) < 7: row.append("")
                    reg_mac = row[6] # G열 기기값
                    if grade == "승인대기": messagebox.showwarning("대기", "관리자 승인이 필요합니다."); return
                    if reg_mac == "": sheet.update_cell(i, 7, my_mac)
                    elif reg_mac != my_mac and "관리자" not in grade:
                        messagebox.showerror("차단", "다른 PC에서 사용 중인 계정입니다."); return
                    
                    rem = "PERMANENT" if raw_period == "영구" else str((datetime.strptime(raw_period, '%Y-%m-%d') - datetime.now()).days + 1)
                    with open(self.settings_file, "w", encoding='utf-8') as f:
                        json.dump({"id": uid if self.save_id_var.get() else "", "pw": upw if self.auto_login_var.get() else "", "save_id": self.save_id_var.get(), "auto_login": self.auto_login_var.get()}, f)
                    self._check_update_after_login(user_name, grade, rem)
                    return
            messagebox.showerror("실패", "계정 정보가 틀립니다.")
        except Exception as e: messagebox.showerror("오류", f"접속 실패: {e}")

    def open_reg(self): # 회원가입 팝업 (생략 - 기존 로직과 동일)
        pop = ctk.CTkToplevel(self)
        pop.title("회원가입 신청")
        pop.geometry("360x420")
        pop.attributes("-topmost", True)

        ctk.CTkLabel(pop, text="회원가입 신청", font=("맑은 고딕", 16, "bold")).pack(pady=(18, 10))
        ctk.CTkLabel(pop, text="신청 후 상태는 '승인대기'로 등록됩니다.", font=("맑은 고딕", 11), text_color="#95a5a6").pack(pady=(0, 10))

        ent_w, ent_h = 280, 42
        rid = ctk.CTkEntry(pop, placeholder_text="아이디", width=ent_w, height=ent_h); rid.pack(pady=6)
        rpw = ctk.CTkEntry(pop, placeholder_text="비밀번호", show="*", width=ent_w, height=ent_h); rpw.pack(pady=6)
        rname = ctk.CTkEntry(pop, placeholder_text="이름(표시명)", width=ent_w, height=ent_h); rname.pack(pady=6)

        status_lbl = ctk.CTkLabel(pop, text="", font=("맑은 고딕", 11), text_color="#bdc3c7")
        status_lbl.pack(pady=(10, 0))

        def submit():
            uid = rid.get().strip()
            pw = rpw.get().strip()
            name = rname.get().strip()
            if not uid or not pw or not name:
                messagebox.showwarning("안내", "아이디/비밀번호/이름을 모두 입력해 주세요.", parent=pop)
                return

            def worker():
                try:
                    status_lbl.configure(text="신청 처리 중...")
                    client = gspread.service_account(filename=os.path.join(BASE_DIR, 'credentials.json'))
                    sheet = client.open_by_key("1I5cdNtpJYQuzYt0juhOcgbcltTv7wb3BJFI2AnI2Crw").sheet1
                    all_data = sheet.get_all_values()

                    # 아이디 중복 체크 (A열)
                    for row in all_data[1:]:
                        if len(row) > 0 and row[0].strip() == uid:
                            self.after(0, lambda: (status_lbl.configure(text=""), messagebox.showerror("중복", "이미 존재하는 아이디입니다.", parent=pop)))
                            return

                    # 시트 컬럼 구조를 그대로 유지하면서 최소 데이터만 채움
                    # A:아이디, B:비밀번호, C:이름, D:(비움), E:등급, F:기간, G:MAC
                    new_row = [uid, pw, name, "", "승인대기", "", ""]
                    sheet.append_row(new_row, value_input_option="RAW")

                    self.after(0, lambda: (status_lbl.configure(text="신청 완료! 관리자 승인 후 이용 가능합니다."), messagebox.showinfo("완료", "회원가입 신청이 완료되었습니다.\n관리자 승인 후 로그인할 수 있습니다.", parent=pop), pop.destroy()))
                except Exception as e:
                    self.after(0, lambda: (status_lbl.configure(text=""), messagebox.showerror("오류", f"신청 실패: {e}", parent=pop)))

            threading.Thread(target=worker, daemon=True).start()

        ctk.CTkButton(pop, text="가입 신청", width=280, height=44, command=submit).pack(pady=(18, 8))
        ctk.CTkButton(pop, text="닫기", width=280, height=40, fg_color="transparent", border_width=1, command=pop.destroy).pack()