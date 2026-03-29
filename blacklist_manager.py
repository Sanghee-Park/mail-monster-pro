import customtkinter as ctk
from tkinter import messagebox, filedialog
import sqlite3
import os


class BlacklistManager(ctk.CTkToplevel):
    """블랙리스트 관리 창 (Task 5-1)"""
    
    def __init__(self, parent, main_ui):
        super().__init__(parent)
        self.main_ui = main_ui
        self.title("블랙리스트 관리")
        self.geometry("600x500")
        self.minsize(420, 360)
        self.resizable(True, True)
        try:
            self.transient(parent)
        except Exception:
            pass

        # 상단: 필드(세로로 쌓여 좁은 창에서도 잘리지 않음)
        top_frame = ctk.CTkFrame(self)
        top_frame.pack(fill="x", padx=10, pady=10)

        row1 = ctk.CTkFrame(top_frame, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(row1, text="새 이메일:").pack(side="left", padx=(0, 8))
        self.email_entry = ctk.CTkEntry(row1, placeholder_text="email@example.com", height=32)
        self.email_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        row2 = ctk.CTkFrame(top_frame, fg_color="transparent")
        row2.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(row2, text="사유:").pack(side="left", padx=(0, 8))
        self.reason_entry = ctk.CTkEntry(row2, height=32)
        self.reason_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row2, text="추가", command=self._add_to_blacklist, width=80, height=32).pack(side="right")
        
        # 중간: 테이블 (트리뷰)
        mid_frame = ctk.CTkFrame(self)
        mid_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 스크롤바와 함께 테이블 
        from tkinter import ttk

        _tv_style = ttk.Style()

        scrollbar = ttk.Scrollbar(mid_frame)
        self.tree = ttk.Treeview(
            mid_frame,
            columns=("email", "reason", "added_at"),
            height=12,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.config(command=self.tree.yview)
        
        self.tree.column("#0", width=0, stretch=False)
        self.tree.column("email", anchor="w", width=220, stretch=True, minwidth=100)
        self.tree.column("reason", anchor="w", width=160, stretch=True, minwidth=80)
        self.tree.column("added_at", anchor="w", width=110, stretch=False, minwidth=90)
        
        self.tree.heading("#0", text="", anchor="w")
        self.tree.heading("email", text="이메일", anchor="w")
        self.tree.heading("reason", text="사유", anchor="w")
        self.tree.heading("added_at", text="추가 날짜", anchor="w")
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        _rz_state = {"aid": None}

        def _apply_blacklist_tree_size():
            _rz_state["aid"] = None
            w = max(mid_frame.winfo_width() - 24, 200)
            em = max(100, int(w * 0.42))
            rs = max(80, int(w * 0.32))
            at = max(90, w - em - rs - 40)
            try:
                self.tree.column("email", width=em)
                self.tree.column("reason", width=rs)
                self.tree.column("added_at", width=at)
            except Exception:
                pass
            try:
                h = max(mid_frame.winfo_height() - 24, 80)
                rh = int(float(_tv_style.lookup("Treeview", "rowheight") or 24))
                rh = max(rh, 18)
                rows = max(6, min(60, h // rh))
                self.tree.configure(height=rows)
            except Exception:
                pass

        def _resize_blacklist_tree(event):
            if event.widget is not mid_frame:
                return
            if _rz_state["aid"] is not None:
                try:
                    mid_frame.after_cancel(_rz_state["aid"])
                except Exception:
                    pass
            _rz_state["aid"] = mid_frame.after(80, _apply_blacklist_tree_size)

        mid_frame.bind("<Configure>", _resize_blacklist_tree, add="+")
        self.after(150, _apply_blacklist_tree_size)

        # 하단: 버튼(한 줄에 너무 많으면 줄바꿈 느낌으로 2행)
        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.pack(fill="x", padx=10, pady=10)
        bf1 = ctk.CTkFrame(bottom_frame, fg_color="transparent")
        bf1.pack(fill="x")
        ctk.CTkButton(bf1, text="선택된 항목 제거", command=self._remove_from_blacklist).pack(side="left", padx=4, pady=2)
        ctk.CTkButton(bf1, text="새로고침", command=self._refresh_table).pack(side="left", padx=4, pady=2)
        ctk.CTkButton(bf1, text="전체 제거", command=self._clear_all).pack(side="left", padx=4, pady=2)
        ctk.CTkButton(bf1, text="닫기", command=self.destroy).pack(side="right", padx=4, pady=2)
        
        # 초기 로드
        self._refresh_table()
    
    def _add_to_blacklist(self):
        """블랙리스트에 이메일 추가"""
        email = self.email_entry.get().strip()
        reason = self.reason_entry.get().strip()
        
        if not email:
            messagebox.showwarning("경고", "이메일을 입력하세요.")
            return
        
        try:
            con = sqlite3.connect(self.main_ui.db_path)
            con.execute(
                "INSERT INTO blacklist(email, reason, added_at) VALUES(?, ?, datetime('now'))",
                (email, reason or "")
            )
            con.commit()
            con.close()
            
            self.email_entry.delete(0, "end")
            self.reason_entry.delete(0, "end")
            self._refresh_table()
            messagebox.showinfo("성공", f"{email}이 블랙리스트에 추가되었습니다.")
        except sqlite3.IntegrityError:
            messagebox.showerror("오류", f"{email}은 이미 블랙리스트에 있습니다.")
        except Exception as e:
            messagebox.showerror("오류", f"추가 중 오류가 발생했습니다:\n{e}")
    
    def _remove_from_blacklist(self):
        """선택된 이메일 제거"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("경고", "제거할 항목을 선택하세요.")
            return
        
        try:
            for item in selection:
                values = self.tree.item(item, "values")
                email = values[0]
                con = sqlite3.connect(self.main_ui.db_path)
                con.execute("DELETE FROM blacklist WHERE email=? COLLATE NOCASE", (email,))
                con.commit()
                con.close()
            
            self._refresh_table()
            messagebox.showinfo("성공", "선택된 항목이 제거되었습니다.")
        except Exception as e:
            messagebox.showerror("오류", f"제거 중 오류가 발생했습니다:\n{e}")
    
    def _clear_all(self):
        """모든 블랙리스트 항목 제거"""
        if messagebox.askyesno("확인", "정말 모든 블랙리스트 항목을 제거하시겠습니까?"):
            try:
                con = sqlite3.connect(self.main_ui.db_path)
                con.execute("DELETE FROM blacklist")
                con.commit()
                con.close()
                
                self._refresh_table()
                messagebox.showinfo("성공", "모든 블랙리스트 항목이 제거되었습니다.")
            except Exception as e:
                messagebox.showerror("오류", f"제거 중 오류가 발생했습니다:\n{e}")
    
    def _refresh_table(self):
        """테이블 새로고침"""
        # 기존 항목 모두 삭제
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        try:
            con = sqlite3.connect(self.main_ui.db_path)
            cur = con.execute("SELECT email, reason, added_at FROM blacklist ORDER BY added_at DESC")
            rows = cur.fetchall()
            con.close()
            
            for row in rows:
                email, reason, added_at = row
                self.tree.insert("", "end", values=(email, reason or "", added_at))
        except Exception as e:
            messagebox.showerror("오류", f"테이블 로드 중 오류가 발생했습니다:\n{e}")
