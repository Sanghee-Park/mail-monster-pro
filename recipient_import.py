"""수신처 엑셀/CSV 파싱. 파일 선택창과 분리해 백그라운드에서 실행한다."""
from __future__ import annotations

import gc
import os
import threading
import traceback
from typing import Callable, List, Sequence


def _cell(row, key, fallback_idx):
    try:
        if hasattr(row, "index") and key in getattr(row, "index", []):
            val = row.get(key, "")
        elif isinstance(row, dict) and key in row:
            val = row.get(key, "")
        else:
            if hasattr(row, "iloc"):
                val = row.iloc[fallback_idx] if len(row) > fallback_idx else ""
            elif isinstance(row, dict):
                vals = list(row.values())
                val = vals[fallback_idx] if len(vals) > fallback_idx else ""
            else:
                val = ""
    except Exception:
        val = ""
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() == "nan" else s


def _rows_from_frame(df) -> tuple[list, list]:
    headers = [str(c) for c in list(df.columns)]
    rows = []
    for _, r in df.iterrows():
        row_data = {}
        for col in df.columns:
            val = r.get(col, "")
            row_data[str(col)] = str(val) if val is not None and str(val).strip() and str(val).strip().lower() != "nan" else ""
        if "업체명" not in row_data or not row_data.get("업체명"):
            row_data["업체명"] = _cell(r, "업체명", 0)
        if "이메일" not in row_data or not row_data.get("이메일"):
            row_data["이메일"] = _cell(r, "이메일", 1)
        rows.append(row_data)
    return rows, headers


def _rows_from_dicts(dicts: List[dict]) -> tuple[list, list]:
    headers = []
    rows = []
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k in d.keys():
            ks = str(k)
            if ks not in headers:
                headers.append(ks)
        row_data = {str(k): ("" if v is None else str(v).strip()) for k, v in d.items()}
        if not row_data.get("업체명"):
            vals = list(d.values())
            row_data["업체명"] = str(vals[0]).strip() if vals else ""
        if not row_data.get("이메일"):
            vals = list(d.values())
            row_data["이메일"] = str(vals[1]).strip() if len(vals) > 1 else ""
        rows.append(row_data)
    return rows, headers


def parse_recipient_excel_files(paths: Sequence[str]) -> dict:
    """UI 스레드 밖에서 호출. pandas가 있으면 xlsx/xls/csv, 없으면 csv만 처리한다."""
    new_rows = []
    merged_headers = []
    loaded_files = 0
    failed_files = []
    for path in paths or []:
        try:
            lower = str(path).lower()
            df = None
            dicts = None
            if lower.endswith(".csv"):
                try:
                    import pandas as pd

                    df = pd.read_csv(path)
                except ImportError:
                    import csv

                    with open(path, newline="", encoding="utf-8-sig") as f:
                        dicts = list(csv.DictReader(f))
            else:
                import pandas as pd

                df = pd.read_excel(path)
            if df is not None:
                rows, headers = _rows_from_frame(df)
                del df
            else:
                rows, headers = _rows_from_dicts(dicts or [])
            loaded_files += 1
            for col in headers:
                if col not in merged_headers:
                    merged_headers.append(col)
            new_rows.extend(rows)
        except Exception as e:
            failed_files.append(f"{os.path.basename(str(path))}: {e}")
            continue
    gc.collect()
    return {
        "rows": new_rows,
        "headers": merged_headers,
        "loaded_files": loaded_files,
        "failed_files": failed_files,
    }


def start_excel_import(
    *,
    dialogs,
    parse_fn: Callable[[list], dict],
    apply_on_ui: Callable[[dict], None],
    spawn: Callable[[Callable[[], None]], None] | None = None,
    key: str = "excel_recipients",
    **dialog_kwargs,
) -> dict:
    """파일 선택만 호출 스레드(UI)에서 하고, 파싱은 spawn된 백그라운드에서 수행한다.

    apply_on_ui 완료를 기다리지 않는다. worker가 이 함수를 직접 부르지 말고
    dialogs.request_on_ui()로 UI에 요청해야 한다.
    """
    paths = dialogs.askopenfilenames(key=key, **dialog_kwargs)
    if not paths:
        return {"started": False, "paths": ()}
    spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def work():
        try:
            result = parse_fn(list(paths))
        except Exception:
            result = {
                "rows": [],
                "headers": [],
                "loaded_files": 0,
                "failed_files": [],
                "error": traceback.format_exc(),
            }
        apply_on_ui(result)

    spawn(work)
    return {"started": True, "paths": tuple(paths)}
