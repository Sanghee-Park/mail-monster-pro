# -*- mode: python ; coding: utf-8 -*-
# MAIL MONSTER PRO V3.0 - PyInstaller spec
# 사용: pyinstaller MAIL_MONSTER_PRO.spec

import os

BASE = os.path.abspath('.')

# 실행 파일과 함께 복사할 데이터 파일
datas = [
    ('wysiwyg_editor.html', '.'),
]
if os.path.exists(os.path.join(BASE, 'pro.ico')):
    datas.append(('pro.ico', '.'))

# 로그인/크레덴셜 등은 실행 시 생성되므로 제외. 사용자가 credentials.json 등은 배포 폴더에 직접 둠.

a = Analysis(
    ['main.py'],
    pathex=[BASE],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'pystray',
        'pystray._win32',
        'login',
        'main_ui',
        'blacklist_manager',
        'gspread',
        'google.auth',
        'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MAIL_MONSTER_PRO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='pro.ico' if os.path.exists(os.path.join(BASE, 'pro.ico')) else None,
)
