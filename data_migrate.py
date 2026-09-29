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
    "send_recovery_journal.json",
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
    block_autosend: bool = False
    needs_choice: bool = False
    choice_note: str = ""
    detail_note: str = ""
    profiles: List[dict] = field(default_factory=list)
    pending_choice: Dict[str, str] = field(default_factory=dict)

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
    copied = list(previously)
    for name in extra:
        if name in SQLITE_SIDECARS:
            continue
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
            if name == "sent_history.db":
                from db_access import backup_database

                _backup_live_db(src_path, dest)
                backup_database(src_path, dest_path)
            else:
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
        from db_access import connect as connect_database
        from json_atomic import StorageWriteError

        con = connect_database(db_path, kind="저장 위치 검사")
        try:
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
        except StorageWriteError:
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
            from db_access import connect as connect_database
            from json_atomic import StorageWriteError

            try:
                con = connect_database(path, kind="발송 기록")
                try:
                    con.execute("BEGIN IMMEDIATE")
                    con.rollback()
                    mode = con.execute("PRAGMA journal_mode").fetchone()
                    if not mode or str(mode[0]).lower() != "wal":
                        return False
                finally:
                    con.close()
            except StorageWriteError as exc:
                if exc.category in ("locked", "busy"):
                    return True
                return False
            except sqlite3.DatabaseError as exc:
                text = str(exc).lower()
                if "not a database" in text or "no such table" in text:
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


class StorageUnavailable(Exception):
    """실행 폴더와 LocalAppData 를 모두 쓸 수 없을 때."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def chosen_data_dir() -> str:
    from app_paths import storage_block_reason

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    global _CHOSEN_DIR
    if override:
        path = os.path.abspath(override)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            path = ""
        if path and not storage_block_reason(path, explicit=True) and storage_root_is_safe(path):
            _CHOSEN_DIR = path
            return path
    elif _CHOSEN_DIR:
        return _CHOSEN_DIR
    prepare_user_data()
    if not _CHOSEN_DIR:
        raise StorageUnavailable("저장 위치를 정하지 못했습니다.")
    return _CHOSEN_DIR


def autosend_blocked() -> bool:
    report = _LAST_REPORT
    return bool(report and report.block_autosend)


def _active_campaign_keys(db_path: str) -> set:
    if not os.path.isfile(db_path):
        return set()
    from db_access import connect as connect_database
    from json_atomic import StorageWriteError

    try:
        con = connect_database(db_path, kind="발송 기록")
        try:
            rows = con.execute(
                """
                SELECT job_id, login_user_id, task_key, status
                FROM campaign_jobs
                WHERE status IN ('queued','running','scheduled_pause','needs_attention','user_stopped')
                """
            ).fetchall()
            return {tuple(row) for row in rows}
        finally:
            con.close()
    except (StorageWriteError, sqlite3.Error):
        return {("unreadable", os.path.abspath(db_path))}


def _backup_live_db(path: str, backup_root: str) -> None:
    if not os.path.isfile(path):
        return
    from db_access import backup_database

    folder = os.path.join(backup_root, "mm-backup")
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    label = "primary" if os.path.normcase(os.path.dirname(path)) == os.path.normcase(backup_root) else "source"
    dest = os.path.join(folder, f"sent_history.{label}.{stamp}.db")
    if os.path.isfile(dest):
        dest = os.path.join(folder, f"sent_history.{label}.{stamp}.{time.time_ns()}.db")
    backup_database(path, dest)


def _inventory_text(label: str, path: str, info: dict) -> str:
    counts = info.get("counts") or {}
    return (
        f"{label}: 발송기록 {counts.get('sent_log', 0)}건, "
        f"작업 {counts.get('campaign_jobs', 0)}건, "
        f"계정작업 {counts.get('campaign_workers', 0)}건, "
        f"대기열 {counts.get('campaign_queue', 0)}건, "
        f"블랙리스트 {counts.get('blacklist', 0)}건, "
        f"대기 수신자 {info.get('pending', 0)}건, "
        f"활성 작업 {len(info.get('active') or [])}건\n{path}"
    )


def _fingerprint_path(folder: str) -> str:
    return os.path.join(folder, "migration_resolved.json")


def _source_fingerprint(path: str) -> str:
    from db_access import database_inventory

    stat = os.stat(path)
    inventory = database_inventory(path)
    blob = repr(
        (
            os.path.abspath(path),
            int(stat.st_size),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            inventory.get("signatures") or {},
        )
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(blob).hexdigest()


def _fingerprint_known(folder: str, source_db: str) -> bool:
    from json_atomic import read_json_object

    data = read_json_object(_fingerprint_path(folder))
    token = _source_fingerprint(source_db)
    return token in {str(item) for item in (data.get("sources") or [])}


def _remember_fingerprint(folder: str, source_db: str) -> None:
    from json_atomic import atomic_write_json, read_json_object

    path = _fingerprint_path(folder)
    data = read_json_object(path)
    sources = [str(item) for item in (data.get("sources") or [])]
    token = _source_fingerprint(source_db)
    if token not in sources:
        sources.append(token)
    atomic_write_json(path, {"sources": sources}, indent=2, ensure_ascii=False, kind="이전 완료 기록")


def _profile_text(primary_info: dict, secondary_info: dict) -> str:
    from db_access import format_database_profile

    return (
        format_database_profile("사용자 폴더", primary_info)
        + "\n\n"
        + format_database_profile("기존 위치", secondary_info)
    )


def _merge_into_local(primary_db: str, secondary_db: str, base_db: str) -> dict:
    from db_access import merge_user_databases

    other = secondary_db if os.path.normcase(base_db) == os.path.normcase(primary_db) else primary_db
    return merge_user_databases(base_db, other, primary_db)


def apply_database_choice(base: str) -> MigrationReport:
    """사용자가 고른 쪽의 활성 캠페인을 기준으로 통합 DB를 만든다."""
    report = _LAST_REPORT or MigrationReport()
    primary_db = report.pending_choice.get("primary_db") or ""
    secondary_db = report.pending_choice.get("secondary_db") or ""
    if base not in ("primary", "secondary") or not (os.path.isfile(primary_db) and os.path.isfile(secondary_db)):
        report.block_autosend = True
        report.needs_choice = True
        return report
    base_db = primary_db if base == "primary" else secondary_db
    try:
        merged = _merge_into_local(primary_db, secondary_db, base_db)
        _remember_fingerprint(os.path.dirname(primary_db), secondary_db)
    except Exception:
        report.failed.append(("sent_history.db", "MergeFailed"))
        report.block_autosend = True
        report.needs_choice = True
        report.detail_note = "발송 기록을 합치지 못했습니다. 원본 파일은 삭제하지 않았고 메일은 보내지 않습니다."
        return report
    report.needs_choice = False
    report.block_autosend = False
    report.detail_note = (
        "선택한 캠페인과 양쪽 발송 기록을 사용자 폴더로 합쳤습니다. 원본 파일은 삭제하지 않았습니다.\n"
        + _profile_text(merged, database_profile_or_empty(secondary_db))
    )
    return report


def database_profile_or_empty(path: str) -> dict:
    from db_access import database_profile

    try:
        return database_profile(path)
    except Exception:
        return {"path": path, "mtime": "", "sent_log": 0, "blacklist": 0, "active_campaigns": 0, "pending": 0, "sending": 0, "needs_review": 0}


def _note_split_databases(primary: str, secondary: str, report: MigrationReport) -> None:
    """내용이 다르면 백업한 뒤, 활성 캠페인이 겹치지 않으면 합치고 겹치면 선택을 받는다."""
    primary_db = os.path.join(primary, "sent_history.db")
    secondary_db = os.path.join(secondary, "sent_history.db")
    if not (os.path.isfile(primary_db) and os.path.isfile(secondary_db)):
        return
    if os.path.normcase(os.path.abspath(primary_db)) == os.path.normcase(os.path.abspath(secondary_db)):
        return
    from db_access import conflicting_task_keys, database_inventory, database_profile

    try:
        if _fingerprint_known(primary, secondary_db):
            return
    except Exception:
        pass
    try:
        left_sig = database_inventory(primary_db)
        right_sig = database_inventory(secondary_db)
        left = database_profile(primary_db)
        right = database_profile(secondary_db)
    except Exception:
        report.failed.append(("sent_history.db", "InventoryFailed"))
        report.block_autosend = True
        report.choice_note = primary
        report.detail_note = "두 발송 기록의 상태를 확인하지 못해 자동발송을 멈췄습니다. 파일은 삭제하지 않았습니다."
        return
    if left_sig.get("signatures") == right_sig.get("signatures") and left_sig.get("ok") and right_sig.get("ok"):
        return
    try:
        _backup_live_db(primary_db, primary)
        _backup_live_db(secondary_db, primary)
    except Exception:
        report.failed.append(("sent_history.db", "BackupFailed"))
        report.block_autosend = True
        report.detail_note = "발송 기록을 백업하지 못해 합치지 않았습니다. 원본 파일은 삭제하지 않았고 메일은 보내지 않습니다."
        return
    report.profiles = [left, right]
    report.pending_choice = {"primary_db": primary_db, "secondary_db": secondary_db}
    report.choice_note = primary
    report.conflicts.append("sent_history.db")
    conflicts = conflicting_task_keys(left, right)
    detail = _profile_text(left, right)
    if conflicts:
        report.needs_choice = True
        report.block_autosend = True
        report.detail_note = (
            "같은 계정에 서로 다른 진행 중 캠페인이 양쪽에 있습니다. "
            "어느 캠페인을 이어갈지 선택하기 전에는 메일을 보내지 않습니다. 원본 파일은 삭제하지 않았습니다.\n"
            + detail
        )
        return
    if right.get("active_campaigns") and not left.get("active_campaigns"):
        base_db = secondary_db
    else:
        base_db = primary_db
    try:
        merged = _merge_into_local(primary_db, secondary_db, base_db)
        _remember_fingerprint(primary, secondary_db)
    except Exception:
        report.failed.append(("sent_history.db", "MergeFailed"))
        report.block_autosend = True
        report.needs_choice = True
        report.detail_note = "발송 기록을 합치지 못했습니다. 원본 파일은 삭제하지 않았고 메일은 보내지 않습니다.\n" + detail
        return
    report.needs_choice = False
    report.block_autosend = False
    report.detail_note = "진행 중 캠페인과 양쪽 발송 기록을 사용자 폴더로 합쳤습니다. 원본 파일은 삭제하지 않았습니다.\n" + _profile_text(merged, right)


def _legacy_candidate_dirs() -> List[str]:
    found = [install_dir()]
    if (os.environ.get(DATA_DIR_ENV) or "").strip():
        return found
    if os.environ.get("MAILMONSTER_SCAN_CLOUD", "1") != "1":
        return found
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    names = []
    for env_name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        base = os.environ.get(env_name) or ""
        if base:
            names.append(base)
            names.append(os.path.join(base, "Desktop"))
            names.append(os.path.join(base, "Documents"))
    names.append(os.path.join(home, "OneDrive", "Desktop"))
    names.append(os.path.join(home, "Desktop"))
    names.append(os.path.join(home, "Documents"))
    for folder in names:
        if folder and os.path.isdir(folder):
            found.append(folder)
    unique = []
    seen = set()
    for folder in found:
        key = os.path.normcase(os.path.abspath(folder))
        if key in seen:
            continue
        seen.add(key)
        unique.append(os.path.abspath(folder))
    return unique


def prepare_user_data(*, force: bool = False) -> MigrationReport:
    """잠금 이후, DB를 열기 전에 LocalAppData 한 곳으로 모은다."""
    from app_paths import local_app_data_dir, storage_block_reason

    global _PREPARED, _LAST_REPORT, _CHOSEN_DIR
    if _PREPARED and not force:
        return _LAST_REPORT or MigrationReport(used_portable=True)
    inst = os.path.abspath(install_dir())
    explicit = ""
    env_dest = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if env_dest:
        candidate = ""
        try:
            candidate = os.path.abspath(env_dest)
            if os.path.exists(candidate) and not os.path.isdir(candidate):
                candidate = ""
            else:
                os.makedirs(candidate, exist_ok=True)
        except OSError:
            candidate = ""
        reason = storage_block_reason(candidate, explicit=True) if candidate else "지정한 데이터 폴더를 만들 수 없습니다."
        if candidate and not reason and storage_root_is_safe(candidate):
            explicit = candidate
        else:
            _CHOSEN_DIR = None
            _PREPARED = True
            _LAST_REPORT = MigrationReport(source_dir=inst, dest_dir=candidate)
            raise StorageUnavailable(
                "MAILMONSTER_DATA_DIR 로 지정한 폴더는 발송 기록 저장 위치로 사용할 수 없습니다.\n"
                f"지정 폴더: {env_dest}\n"
                f"사유: {reason or '데이터베이스 쓰기를 확인하지 못했습니다.'}\n"
                "OneDrive, 네트워크, 실행 파일 폴더는 사용할 수 없습니다. 기존 파일은 삭제하지 않았습니다."
            )
    dest = explicit or local_app_data_dir()
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        dest = ""
    block = storage_block_reason(dest, explicit=bool(explicit)) if dest else "사용자 폴더를 만들 수 없습니다."
    if block or not dest or not storage_root_is_safe(dest):
        _CHOSEN_DIR = None
        _PREPARED = True
        _LAST_REPORT = MigrationReport(source_dir=inst, dest_dir=dest)
        raise StorageUnavailable(
            "발송 기록을 저장할 안전한 폴더가 없습니다.\n\n"
            f"실행 폴더: {inst}\n"
            f"대상 폴더: {dest or '(없음)'}\n"
            f"사유: {block or '데이터베이스 쓰기를 확인하지 못했습니다.'}\n"
            "기존 파일은 삭제하지 않았습니다. OneDrive·바탕화면·네트워크 폴더는 사용하지 않습니다."
        )
    _CHOSEN_DIR = dest
    report = MigrationReport(used_portable=False, source_dir=inst, dest_dir=dest)
    for source in _legacy_candidate_dirs():
        if os.path.normcase(source) == os.path.normcase(dest):
            continue
        has_state = any(os.path.isfile(os.path.join(source, name)) for name in MIGRATABLE_FILES)
        if not has_state:
            continue
        part = migrate_legacy_data_files(source, dest, source_writable=False, copy_fn=_backup_then_copy)
        report.migrated.extend(part.migrated)
        report.conflicts.extend(part.conflicts)
        report.failed.extend(part.failed)
        report.skipped_existing.extend(part.skipped_existing)
        _note_split_databases(dest, source, report)
        if report.needs_choice or report.block_autosend:
            break
    if not storage_root_is_safe(dest):
        _CHOSEN_DIR = None
        _PREPARED = True
        _LAST_REPORT = report
        raise StorageUnavailable(
            "사용자 폴더로 옮긴 뒤에도 데이터베이스 쓰기를 확인하지 못했습니다.\n"
            f"대상 폴더: {dest}\n기존 파일은 삭제하지 않았습니다."
        )
    _LAST_REPORT = report
    _PREPARED = True
    return report


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
    if report.block_autosend and report.detail_note:
        lines.append(report.detail_note)
    return "\n".join(lines)
