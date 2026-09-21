import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign_store import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_NEEDS_ATTENTION,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SCHEDULED_PAUSE,
    JOB_USER_STOPPED,
)
from campaign_ui_state import button_state_for_status, event_matches_current


class CampaignUiStateTests(unittest.TestCase):
    def test_button_state_matrix(self):
        cases = {
            "": ("normal", "disabled", "disabled", False),
            JOB_QUEUED: ("disabled", "normal", "normal", False),
            JOB_RUNNING: ("disabled", "normal", "normal", False),
            JOB_SCHEDULED_PAUSE: ("disabled", "normal", "normal", False),
            JOB_USER_STOPPED: ("normal", "disabled", "normal", False),
            JOB_NEEDS_ATTENTION: ("disabled", "disabled", "normal", True),
            "needs_review": ("disabled", "disabled", "normal", True),
            JOB_COMPLETED: ("normal", "disabled", "disabled", False),
            JOB_CANCELLED: ("normal", "disabled", "disabled", False),
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                state = button_state_for_status(status)
                self.assertEqual(
                    (
                        state["start_state"],
                        state["stop_state"],
                        state["cancel_state"],
                        state["fix_visible"],
                    ),
                    expected,
                )
        self.assertEqual(button_state_for_status(JOB_USER_STOPPED)["start_text"], "▶ 발송 재개")

    def test_other_account_event_cannot_change_selected_account(self):
        self.assertFalse(
            event_matches_current(
                event_task_key="네이버_2",
                event_job_id="job-b",
                event_generation=1,
                current_task_key="네이버_1",
                current_job_id="job-a",
                current_generation=1,
            )
        )

    def test_old_job_callback_cannot_change_new_job(self):
        self.assertFalse(
            event_matches_current(
                event_task_key="네이버_1",
                event_job_id="old-job",
                event_generation=1,
                current_task_key="네이버_1",
                current_job_id="new-job",
                current_generation=2,
            )
        )
        self.assertTrue(
            event_matches_current(
                event_task_key="네이버_1",
                event_job_id="new-job",
                event_generation=2,
                current_task_key="네이버_1",
                current_job_id="new-job",
                current_generation=2,
            )
        )


if __name__ == "__main__":
    unittest.main()
