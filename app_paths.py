"""설치 폴더·사용자 데이터 폴더 절대경로.

HKCU Run으로 실행되면 현재 작업 디렉터리가 프로그램 폴더가 아닐 수 있으므로
작업 디렉터리 상대경로를 쓰지 않는다.
EXE는 sys.executable, 개발 환경은 __file__ 기준이다.
Program Files처럼 쓰기가 막힌 위치면 %LOCALAPPDATA%\\MAIL_MONSTER_PRO 를 사용한다.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

APP_DATA_FOLDER = "MAIL_MONSTER_PRO"
DATA_DIR_ENV = "MAILMONSTER_DATA_DIR"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir() -> str:
    """PyInstaller가 풀은 읽기 전용 리소스(예: extra_holidays.example.json)."""
    if is_frozen():
        return os.path.abspath(getattr(sys, "_MEIPASS", install_dir()))
    return install_dir()


def _dir_is_writable(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".mm_write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass
        return False


def user_data_dir() -> str:
    from data_migrate import prepare_user_data

    prepare_user_data()
    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        abs_d = os.path.abspath(override)
        os.makedirs(abs_d, exist_ok=True)
        return abs_d
    inst = install_dir()
    if _dir_is_writable(inst):
        return inst
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    target = os.path.join(local, APP_DATA_FOLDER)
    os.makedirs(target, exist_ok=True)
    return os.path.abspath(target)


def data_file(name: str) -> str:
    """생성·갱신하는 설정/DB 파일의 절대경로."""
    return os.path.join(user_data_dir(), name)


def find_existing_file(name: str) -> str:
    """기존 파일이 있으면 그 위치를 쓰고, 없으면 사용자 데이터 폴더 경로를 반환."""
    for folder in (user_data_dir(), install_dir()):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return data_file(name)


def example_file(name: str) -> str:
    return os.path.join(resource_dir(), name)


def bundled_file(name: str) -> str:
    """설치 폴더 또는 번들 리소스에서 읽기 (credentials.json, ico, 예시 JSON)."""
    inst = os.path.join(install_dir(), name)
    if os.path.isfile(inst):
        return inst
    res = os.path.join(resource_dir(), name)
    if os.path.isfile(res):
        return res
    return inst


def writable_file(name: str) -> str:
    """기존 파일이 있고 그 경로에 쓸 수 있으면 유지, 아니면 사용자 데이터 폴더."""
    existing = find_existing_file(name)
    if os.path.isfile(existing):
        folder = os.path.dirname(existing)
        if _dir_is_writable(folder):
            return existing
        return data_file(name)
    preferred = data_file(name)
    parent = os.path.dirname(preferred)
    if _dir_is_writable(parent) or parent == user_data_dir():
        return preferred
    return data_file(name)


def extra_holidays_user_path() -> str:
    existing = find_existing_file("extra_holidays.json")
    if os.path.isfile(existing):
        folder = os.path.dirname(existing)
        if _dir_is_writable(folder):
            return existing
    return data_file("extra_holidays.json")


def extra_holidays_example_path() -> str:
    return example_file("extra_holidays.example.json")


def resolve_state_files() -> dict:
    """설정·DB 절대경로. cwd와 무관."""
    return {
        "sent_history.db": writable_file("sent_history.db"),
        "login_settings.json": writable_file("login_settings.json"),
        "config.json": writable_file("config.json"),
        "recipients.json": writable_file("recipients.json"),
        "templates.json": writable_file("templates.json"),
        "user_profiles.json": writable_file("user_profiles.json"),
        "extra_holidays.json": extra_holidays_user_path(),
        "credentials.json": bundled_file("credentials.json"),
    }
