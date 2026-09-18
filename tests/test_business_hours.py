import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_hours import KST, BusinessHours, POLICY_TEXT, as_kst, load_extra_holiday_dates


def kst(y, m, d, hh=10, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST)


class BusinessHoursTests(unittest.TestCase):
    def setUp(self):
        self.hours = BusinessHours(extra_dates=set())

    def test_policy_text(self):
        self.assertIn("09:00~18:00", POLICY_TEXT)

    def test_now_is_kst_not_naive(self):
        now = self.hours.now()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(str(now.tzinfo), str(KST))

    def test_naive_datetime_interpreted_as_kst(self):
        naive = datetime(2026, 9, 16, 9, 0, 0)
        self.assertTrue(self.hours.is_send_allowed(naive))

    def test_utc_nine_is_not_kst_nine(self):
        from datetime import timezone

        utc_00 = datetime(2026, 9, 16, 0, 0, 0, tzinfo=timezone.utc)  # 09:00 KST
        self.assertTrue(self.hours.is_send_allowed(utc_00))
        utc_09 = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)  # 18:00 KST
        self.assertFalse(self.hours.is_send_allowed(utc_09))

    def test_kst_9am_exact_allowed(self):
        self.assertTrue(self.hours.is_send_allowed(kst(2026, 9, 16, 9, 0, 0)))

    def test_kst_6pm_exact_not_allowed(self):
        self.assertFalse(self.hours.is_send_allowed(kst(2026, 9, 16, 18, 0, 0)))

    def test_weekday_allowed(self):
        # 2026-09-16 수요일 평일
        self.assertTrue(self.hours.is_business_day(kst(2026, 9, 16).date()))
        self.assertTrue(self.hours.is_send_allowed(kst(2026, 9, 16, 10, 0)))

    def test_saturday_not_allowed(self):
        self.assertFalse(self.hours.is_send_allowed(kst(2026, 9, 19, 10, 0)))

    def test_sunday_not_allowed(self):
        self.assertFalse(self.hours.is_send_allowed(kst(2026, 9, 20, 10, 0)))

    def test_korean_public_holiday_not_allowed(self):
        kr = self.hours.holiday_calendar(2026)
        self.assertIsNotNone(kr)
        found = None
        for d, name in kr.items():
            if d.year == 2026 and ("삼일" in str(name) or "Independence" in str(name)):
                found = d
                break
        self.assertIsNotNone(found, "holidays.KR에 삼일절이 있어야 합니다")
        self.assertFalse(self.hours.is_send_allowed(kst(found.year, found.month, found.day, 10, 0)))

    def test_substitute_holiday_not_allowed(self):
        kr = self.hours.holiday_calendar(2026)
        self.assertIsNotNone(kr)
        alts = []
        for d, name in kr.items():
            n = str(name)
            if d.year != 2026:
                continue
            if "대체" in n or "Alternative" in n or "observed" in n.lower() or "Substitute" in n:
                alts.append((d, n))
        self.assertTrue(alts, "대체공휴일이 holidays 패키지에 있어야 합니다")
        d = alts[0][0]
        self.assertFalse(self.hours.is_send_allowed(kst(d.year, d.month, d.day, 11, 0)))

    def test_workers_day_not_allowed(self):
        self.assertTrue(self.hours.is_workers_day(kst(2026, 5, 1).date()))
        self.assertFalse(self.hours.is_send_allowed(kst(2026, 5, 1, 10, 0)))

    def test_extra_holiday_not_allowed(self):
        hours = BusinessHours(extra_dates={kst(2026, 9, 16).date()})
        self.assertFalse(hours.is_send_allowed(kst(2026, 9, 16, 10, 0)))

    def test_load_extra_holidays_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "extra_holidays.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"dates": ["2026-10-02"], "notes": {"2026-10-02": "임시공휴일"}}')
            dates = load_extra_holiday_dates(p)
            self.assertIn(kst(2026, 10, 2).date(), dates)
            hours = BusinessHours(extra_path=p)
            self.assertFalse(hours.is_send_allowed(kst(2026, 10, 2, 10, 0)))

    def test_next_business_day_9am_from_saturday(self):
        nxt = self.hours.next_send_window_start(kst(2026, 9, 19, 11, 0))
        self.assertEqual(nxt, kst(2026, 9, 21, 9, 0, 0))  # 월요일

    def test_next_window_before_9am_same_day(self):
        nxt = self.hours.next_send_window_start(kst(2026, 9, 16, 8, 30))
        self.assertEqual(nxt, kst(2026, 9, 16, 9, 0, 0))

    def test_next_window_after_6pm(self):
        nxt = self.hours.next_send_window_start(kst(2026, 9, 16, 18, 0))
        self.assertEqual(nxt, kst(2026, 9, 17, 9, 0, 0))

    def test_format_resume_text(self):
        text = self.hours.format_resume_text(kst(2026, 9, 18, 9, 0))
        self.assertEqual(text, "업무시간 외 자동 대기 중 · 다음 발송: 2026-09-18 09:00 KST")


if __name__ == "__main__":
    unittest.main()
