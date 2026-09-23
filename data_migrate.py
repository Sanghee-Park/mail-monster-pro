"""Program Files 등 읽기 전용 설치 폴더의 v2.7.3 데이터를 LocalAppData로 1회 복사.

설치 폴더가 쓰기 가능하면(포터블) 이동하지 않는다.
대상에 파일이 있으면 덮어쓰지 않는다. 원본은 삭제·이동하지 않는다.
로그/안내에 비밀번호·토큰·파일 내용을 넣지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app_paths import (
    APP_DATA_FOLDER,
    DATA_DIR_ENV,
    _dir_is_writable,
    install_dir,
)
from json_atomic import clear_readonly, file_allows_write

MIGRATABLE_FILES: Tuple[str, ...] = (
    "sent_history.db",
    "login_settings.json",
    "config.json",
    "recipients.json",
    "templates.json",
    "user_profiles.json",
    "extra_holidays.json",
)

SQLITE_SIDECARS = ("sent_history.db-wal", "sent_history.db-shm")
MARKER_NAME = ".mm_data_origin.json"

CopyFn = Callable[[str, str], None]


@dataclass
class MigrationReport:
    used_portable: bool = False
    source_dir: str = ""
    dest_dir: str = ""
    migrated: List[str] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)
    already_migrated: bool = False

    @property
    def has_user_notice(self) -> bool:
        return bool(self.migrated or self.conflicts or self.failed)


_PREPARED = False
_LAST_REPORT: Optional[MigrationReport] = None
_CHOSEN_DIR: Optional[str] = None


def reset_prepare_cache() -> None:
    global _PREPARED, _LAST_REPORT, _CHOSEN_DIR
    _PREPARED = False
    _LAST_REPORT = None
    _CHOSEN_DIR = None


def last_migration_report() -> Optional[MigrationReport]:
    return _LAST_REPORT


def appdata_target_dir() -> str:
    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        abs_d = os.path.abspath(override)
        os.makedirs(abs_d, exist_ok=True)
        return abs_d
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    target = os.path.join(local, APP_DATA_FOLDER)
    os.makedirs(target, exist_ok=True)
    return os.path.abspath(target)


def copy_file_atomic(src: str, dest: str) -> None:
    """임시 파일에 복사한 뒤 os.replace. 실패 시 임시 파일만 지우고 원본·대상은 유지."""
    dest_dir = os.path.dirname(dest) or "."
    os.makedirs(dest_dir, exist_ok=True)
    tmp = dest + ".mm_migrating"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
        clear_readonly(dest)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _read_marker(dest_dir: str) -> dict:
    path = os.path.join(dest_dir, MARKER_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_marker(dest_dir: str, payload: dict) -> None:
    path = os.path.join(dest_dir, MARKER_NAME)
    tmp = path + ".tmp"
    safe = {
        "source_dir": str(payload.get("source_dir") or ""),
        "copied": list(payload.get("copied") or []),
        "conflicts": list(payload.get("conflicts") or []),
    }
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(safe, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def migrate_legacy_data_files(
    source_dir: str,
    dest_dir: str,
    *,
    source_writable: Optional[bool] = None,
    names: Sequence[str] = MIGRATABLE_FILES,
    copy_fn: Optional[CopyFn] = None,
) -> MigrationReport:
    """source_writable=True 이면 포터블로 보고 복사하지 않는다."""
    src = os.path.abspath(source_dir)
    dest = os.path.abspath(dest_dir)
    report = MigrationReport(source_dir=src, dest_dir=dest)
    writable = _dir_is_writable(src) if source_writable is None else bool(source_writable)
    if writable:
        report.used_portable = True
        report.dest_dir = src
        return report
    if os.path.normcase(src) == os.path.normcase(dest):
        report.used_portable = True
        return report

    os.makedirs(dest, exist_ok=True)
    marker = _read_marker(dest)
    previously = {str(x) for x in (marker.get("copied") or [])}
    if previously:
        report.already_migrated = True
    do_copy = copy_fn or copy_file_atomic
    extra = list(names)
    if "sent_history.db" in extra:
        extra = list(extra) + [s for s in SQLITE_SIDECARS if s not in extra]

    copied = list(previously)
    for name in extra:
        src_path = os.path.join(src, name)
        dest_path = os.path.join(dest, name)
        if not os.path.isfile(src_path):
            continue
        if os.path.isfile(dest_path):
            report.skipped_existing.append(name)
            if name not in previously:
                report.conflicts.append(name)
            continue
        try:
            do_copy(src_path, dest_path)
            report.migrated.append(name)
            if name not in copied:
                copied.append(name)
        except Exception as exc:
            err_name = type(exc).__name__
            report.failed.append((name, err_name))
            try:
                if os.path.isfile(dest_path + ".mm_migrating"):
                    os.remove(dest_path + ".mm_migrating")
            except OSError:
                pass

    marker_out = {
        "source_dir": src,
        "copied": copied,
        "conflicts": list(dict.fromkeys(list(marker.get("conflicts") or []) + report.conflicts)),
    }
    _write_marker(dest, marker_out)
    return report


def _remove_probe_files(path: str) -> None:
    for extra in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(extra):
                clear_readonly(extra)
                os.remove(extra)
        except OSError:
            pass


def directory_supports_replace_and_wal(directory: str) -> bool:
    """폴더에 파일을 만들고, 기존 파일을 교체하고, SQLite WAL을 쓸 수 있는지 확인한다."""
    if not directory or not _dir_is_writable(directory):
        return False
    probe = os.path.join(directory, ".mm_replace_probe.json")
    tmp = probe + ".tmp"
    db_path = os.path.join(directory, ".mm_storage_probe.db")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write('{"probe":1}')
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write('{"probe":2}')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, probe)
        with open(probe, "r", encoding="utf-8") as handle:
            replaced = handle.read()
        if '"probe": 2' not in replaced and '"probe":2' not in replaced:
            return False
        _remove_probe_files(db_path)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE probe(id INTEGER)")
            con.execute("INSERT INTO probe(id) VALUES (1)")
            con.commit()
            mode = con.execute("PRAGMA journal_mode").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                return False
            if not (os.path.isfile(db_path + "-wal") or os.path.isfile(db_path + "-shm")):
                return False
        finally:
            con.close()
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        for extra in (probe, tmp):
            try:
                if os.path.exists(extra):
                    os.remove(extra)
            except OSError:
                pass
        _remove_probe_files(db_path)


def existing_state_is_writable(directory: str) -> bool:
    for name in list(MIGRATABLE_FILES) + list(SQLITE_SIDECARS):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if not clear_readonly(path) or not file_allows_write(path):
            return False
        if name == "sent_history.db":
            try:
                con = sqlite3.connect(path, timeout=5)
                try:
                    con.execute("BEGIN IMMEDIATE")
                    con.commit()
                    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()
                    if not mode or str(mode[0]).lower() != "wal":
                        return False
                finally:
                    con.close()
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    return True
                return False
            except sqlite3.Error:
                return False
    return True


def storage_root_is_safe(directory: str) -> bool:
    return directory_supports_replace_and_wal(directory) and existing_state_is_writable(directory)


def _backup_then_copy(src: str, dest: str) -> None:
    backup_dir = os.path.join(os.path.dirname(dest), "mm-backup")
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, f"{os.path.basename(dest)}.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(src, backup)
    clear_readonly(backup)
    copy_file_atomic(src, dest)


def chosen_data_dir() -> str:
    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        path = os.path.abspath(override)
        os.makedirs(path, exist_ok=True)
        return path
    global _CHOSEN_DIR
    if _CHOSEN_DIR:
        return _CHOSEN_DIR
    prepare_user_data()
    return _CHOSEN_DIR or os.path.abspath(install_dir())


def prepare_user_data(*, force: bool = False) -> MigrationReport:
    """저장 위치를 프로세스당 한 번 정한다. 안전하지 않으면 LocalAppData로 복사한다."""
    global _PREPARED, _LAST_REPORT, _CHOSEN_DIR
    if _PREPARED and not force:
        return _LAST_REPORT or MigrationReport(used_portable=True)
    inst = os.path.abspath(install_dir())
    env_dest = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if env_dest:
        dest = appdata_target_dir()
        _CHOSEN_DIR = dest
        if os.path.normcase(inst) == os.path.normcase(dest) or storage_root_is_safe(inst):
            _LAST_REPORT = MigrationReport(used_portable=True, source_dir=inst, dest_dir=dest)
        else:
            _LAST_REPORT = migrate_legacy_data_files(
                inst, dest, source_writable=False, copy_fn=_backup_then_copy
            )
        _PREPARED = True
        return _LAST_REPORT
    if storage_root_is_safe(inst):
        _CHOSEN_DIR = inst
        _LAST_REPORT = MigrationReport(used_portable=True, source_dir=inst, dest_dir=inst)
        _PREPARED = True
        return _LAST_REPORT
    dest = appdata_target_dir()
    report = migrate_legacy_data_files(inst, dest, source_writable=False, copy_fn=_backup_then_copy)
    if storage_root_is_safe(dest):
        _CHOSEN_DIR = dest
    else:
        _CHOSEN_DIR = inst
        report.failed.append(("storage-root", "UnsafeDestination"))
    _LAST_REPORT = report
    _PREPARED = True
    return _LAST_REPORT


def format_migration_user_message(report: Optional[MigrationReport]) -> str:
    """파일 내용·비밀값은 넣지 않는다."""
    if not report or report.used_portable:
        return ""
    lines: List[str] = []
    if report.migrated:
        lines.append(
            "이전 설치 폴더의 데이터를 사용자 폴더로 복사했습니다. 원본은 그대로 두었습니다."
        )
        lines.append(f"원본 위치: {report.source_dir}")
        lines.append(f"새 위치: {report.dest_dir}")
        lines.append("복사된 파일: " + ", ".join(report.migrated))
    if report.conflicts:
        lines.append(
            "설치 폴더와 사용자 폴더에 같은 이름의 파일이 있어 사용자 폴더 파일을 사용합니다. 덮어쓰지 않았습니다."
        )
        lines.append(f"사용자 폴더: {report.dest_dir}")
        lines.append(f"설치 폴더: {report.source_dir}")
        lines.append("해당 파일: " + ", ".join(report.conflicts))
    if report.failed:
        names = ", ".join(n for n, _ in report.failed)
        lines.append("일부 파일을 복사하지 못했습니다. 원본은 삭제되지 않았습니다.")
        lines.append(f"원본 위치: {report.source_dir}")
        lines.append(f"대상 위치: {report.dest_dir}")
        lines.append("실패 파일: " + names)
    return "\n".join(lines)
