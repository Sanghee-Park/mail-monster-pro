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
        self.resizable(True, True)
        
        # 상단: 추가/제거 버튼
        top_frame = ctk.CTkFrame(self)
        top_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkLabel(top_frame, text="새 이메일:").pack(side="left", padx=5)
        self.email_entry = ctk.CTkEntry(top_frame, width=250)
        self.email_entry.pack(side="left", padx=5)
        
        ctk.CTkLabel(top_frame, text="사유:").pack(side="left", padx=5)
        self.reason_entry = ctk.CTkEntry(top_frame, width=150)
        self.reason_entry.pack(side="left", padx=5)
        
        ctk.CTkButton(top_frame, text="추가", command=self._add_to_blacklist, width=80).pack(side="left", padx=5)
        
        # 중간: 테이블 (트리뷰)
        mid_frame = ctk.CTkFrame(self)
        mid_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 스크롤바와 함께 테이블 
        from tkinter import ttk
        
        # 스크롤바
        scrollbar = ttk.Scrollbar(mid_frame)
        scrollbar.pack(side="right", fill="y")
        
        # 트리뷰
        self.tree = ttk.Treeview(
            mid_frame,
            columns=("email", "reason", "added_at"),
            height=15,
            yscrollcommand=scrollbar.set
        )
        scrollbar.config(command=self.tree.yview)
        
        self.tree.column("#0", width=0, stretch="no")
        self.tree.column("email", anchor="w", width=250)
        self.tree.column("reason", anchor="w", width=200)
        self.tree.column("added_at", anchor="w", width=120)
        
        self.tree.heading("#0", text="", anchor="w")
        self.tree.heading("email", text="이메일", anchor="w")
        self.tree.heading("reason", text="사유", anchor="w")
        self.tree.heading("added_at", text="추가 날짜", anchor="w")
        
        self.tree.pack(fill="both", expand=True)
        
        # 하단: 선택된 항목 삭제 버튼
        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.pack(fill="x", padx=10, pady=10)
        
        ctk.CTkButton(bottom_frame, text="선택된 항목 제거", command=self._remove_from_blacklist, width=150).pack(side="left", padx=5)
        ctk.CTkButton(bottom_frame, text="새로고침", command=self._refresh_table, width=150).pack(side="left", padx=5)
        ctk.CTkButton(bottom_frame, text="전체 제거", command=self._clear_all, width=150).pack(side="left", padx=5)
        ctk.CTkButton(bottom_frame, text="닫기", command=self.destroy, width=150).pack(side="right", padx=5)
        
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
