"""대한민국 영업일·발송 가능 시간 판정 (항상 KST, PC 로컬 시간대와 무관).

발송 가능: 대한민국 영업일 AND 09:00 <= 현재 KST < 18:00
영업일: 월~금, 주말/법정공휴일/대체공휴일/근로자의 날/임시공휴일(라이브러리·수동 목록) 제외.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Optional, Set, Union
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SEND_START = time(9, 0, 0)
SEND_END = time(18, 0, 0)
WORKERS_DAY_MONTH = 5
WORKERS_DAY_DAY = 1
POLICY_TEXT = "대한민국 영업일 · 공휴일 제외 · 09:00~18:00"
EXTRA_HOLIDAYS_FILENAME = "extra_holidays.json"

NowFn = Callable[[], datetime]


def as_kst(dt: Optional[datetime] = None) -> datetime:
    """타임존이 없으면 KST로 해석하고, 있으면 KST로 변환한다. now는 datetime.now(KST)."""
    if dt is None:
        return datetime.now(KST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def parse_iso_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def load_extra_holiday_dates(path: Optional[str] = None) -> Set[date]:
    """수동 휴무일. JSON 형식:
    - {"dates": ["2026-10-02", ...], "notes": {"2026-10-02": "임시공휴일"}}
    - {"2026-10-02": "임시공휴일"}
    - ["2026-10-02"]
    """
    found: Set[date] = set()
    if not path or not os.path.isfile(path):
        return found
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return found

    def _add(item) -> None:
        d = parse_iso_date(str(item))
        if d:
            found.add(d)

    if isinstance(raw, list):
        for item in raw:
            _add(item)
        return found
    if isinstance(raw, dict):
        dates = raw.get("dates")
        if isinstance(dates, list):
            for item in dates:
                _add(item)
        for key, val in raw.items():
            if key in ("dates", "notes", "comment", "description"):
                continue
            _add(key)
            if isinstance(val, str):
                _add(val)
    return found


def default_extra_holidays_path(base_dir: str) -> str:
    return os.path.join(base_dir, EXTRA_HOLIDAYS_FILENAME)


def ensure_extra_holidays_file(path: str) -> None:
    if os.path.isfile(path):
        return
    payload = {
        "dates": [],
        "notes": {},
        "description": "라이브러리에 아직 없는 임시공휴일·선거일 등을 YYYY-MM-DD로 추가하세요.",
    }
    dname = os.path.dirname(path) or "."
    os.makedirs(dname, exist_ok=True)
    from json_atomic import atomic_write_json

    atomic_write_json(path, payload, indent=2, ensure_ascii=False, kind="임시공휴일 목록")


def _build_kr_calendar(years: Optional[Iterable[int]] = None):
    try:
        import holidays
    except ImportError:
        return None
    year_list = list(years) if years is not None else None
    if hasattr(holidays, "country_holidays"):
        if year_list:
            return holidays.country_holidays("KR", years=year_list)
        return holidays.country_holidays("KR")
    kr_cls = getattr(holidays, "KR", None)
    if kr_cls is None:
        return None
    if year_list:
        return kr_cls(years=year_list)
    return kr_cls()


class BusinessHours:
    def __init__(
        self,
        extra_dates: Optional[Iterable[date]] = None,
        extra_path: Optional[str] = None,
        holiday_calendar=None,
        now_fn: Optional[NowFn] = None,
    ):
        self.extra_path = extra_path
        self._extra_override: Optional[Set[date]] = set(extra_dates) if extra_dates is not None else None
        self._calendar = holiday_calendar
        self.now_fn = now_fn or (lambda: datetime.now(KST))

    def now(self) -> datetime:
        return as_kst(self.now_fn())

    def extra_dates(self) -> Set[date]:
        if self._extra_override is not None:
            return set(self._extra_override)
        return load_extra_holiday_dates(self.extra_path)

    def holiday_calendar(self, year: int):
        if self._calendar is not None:
            return self._calendar
        return _build_kr_calendar(years=[year, year - 1, year + 1])

    def is_workers_day(self, d: date) -> bool:
        return d.month == WORKERS_DAY_MONTH and d.day == WORKERS_DAY_DAY

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def holiday_name(self, d: date) -> Optional[str]:
        if self.is_workers_day(d):
            return "근로자의 날"
        extra = self.extra_dates()
        if d in extra:
            return "수동 휴무일"
        cal = self.holiday_calendar(d.year)
        if cal is None:
            return None
        try:
            name = cal.get(d)
        except Exception:
            name = None
        if name:
            return str(name)
        if d in cal:
            return "공휴일"
        return None

    def is_public_holiday(self, d: date) -> bool:
        """법정공휴일·대체공휴일·선거일 등 holidays 패키지가 제공하는 휴일."""
        cal = self.holiday_calendar(d.year)
        if cal is None:
            return False
        try:
            return d in cal
        except Exception:
            return False

    def is_non_working_holiday(self, d: date) -> bool:
        return self.is_workers_day(d) or (d in self.extra_dates()) or self.is_public_holiday(d)

    def is_business_day(self, d: Union[date, datetime]) -> bool:
        if isinstance(d, datetime):
            d = as_kst(d).date()
        if self.is_weekend(d):
            return False
        if self.is_non_working_holiday(d):
            return False
        return True

    def is_send_allowed(self, dt: Optional[datetime] = None) -> bool:
        now = as_kst(dt if dt is not None else self.now())
        if not self.is_business_day(now.date()):
            return False
        t = now.time()
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        return SEND_START <= t < SEND_END

    def next_send_window_start(self, dt: Optional[datetime] = None) -> datetime:
        """현재 시각 이후 다음 발송 시작 시각(영업일 09:00 KST).
        오늘 09:00 이전이면 오늘 09:00(영업일인 경우). 그 외는 다음 영업일 09:00.
        """
        now = as_kst(dt if dt is not None else self.now())
        today = now.date()
        if self.is_business_day(today) and now.time() < SEND_START:
            return datetime.combine(today, SEND_START, tzinfo=KST)
        d = today + timedelta(days=1)
        for _ in range(800):
            if self.is_business_day(d):
                return datetime.combine(d, SEND_START, tzinfo=KST)
            d += timedelta(days=1)
        raise RuntimeError("다음 영업일을 찾을 수 없습니다.")

    def format_resume_text(self, dt: Optional[datetime] = None) -> str:
        when = as_kst(dt if dt is not None else self.next_send_window_start())
        return f"업무시간 외 자동 대기 중 · 다음 발송: {when.strftime('%Y-%m-%d %H:%M')} KST"
