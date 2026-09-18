# -*- mode: python ; coding: utf-8 -*-
# MAIL MONSTER PRO V3.0 - PyInstaller spec
# 사용: pyinstaller MAIL_MONSTER_PRO.spec

import os

try:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules
except Exception:
    collect_data_files = None
    collect_submodules = None

BASE = os.path.abspath('.')

# 실행 파일과 함께 복사할 데이터 파일
datas = [
    ('wysiwyg_editor.html', '.'),
]
if os.path.exists(os.path.join(BASE, 'pro.ico')):
    datas.append(('pro.ico', '.'))
if os.path.exists(os.path.join(BASE, 'extra_holidays.example.json')):
    datas.append(('extra_holidays.example.json', '.'))

if collect_data_files:
    for pkg in ("tzdata", "holidays"):
        try:
            datas += collect_data_files(pkg)
        except Exception:
            pass

hiddenimports = [
    'customtkinter',
    'PIL',
    'PIL._tkinter_finder',
    'pystray',
    'pystray._win32',
    'login',
    'main_ui',
    'blacklist_manager',
    'business_hours',
    'campaign_store',
    'campaign_runtime',
    'app_paths',
    'version_compare',
    'smtp_credentials',
    'ui_safe',
    'login_network',
    'campaign_attachments',
    'campaign_attention',
    'data_migrate',
    'holidays',
    'tzdata',
    'zoneinfo',
    'gspread',
    'google.auth',
    'requests',
]
if collect_submodules:
    for pkg in ("holidays", "tzdata"):
        try:
            hiddenimports += collect_submodules(pkg)
        except Exception:
            pass

# 로그인/크레덴셜 등은 실행 시 생성되므로 제외. 사용자가 credentials.json 등은 배포 폴더에 직접 둠.

a = Analysis(
    ['main.py'],
    pathex=[BASE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
