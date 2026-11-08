"""On Windows Path.home() reads USERPROFILE, not HOME, so without this the tests would write into
the real ~/.claude, and a stub without an extension would lose to the real herdr.exe on PATH."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

if os.name == "nt":
    # Before any test module loads: some read Path.home() at import.
    os.environ["USERPROFILE"] = os.environ["HOME"] = tempfile.mkdtemp(prefix="orch-tests-home-")
    _setenv = pytest.MonkeyPatch.setenv

    def setenv(self, name, value, prepend=None):
        if name == "HOME":
            _setenv(self, "USERPROFILE", value, prepend)
        _setenv(self, name, value, prepend)

    pytest.MonkeyPatch.setenv = setenv
    _write_text = Path.write_text

    def write_text(self, data, *args, **kw):
        """A test's stub program `#!python ...` needs a .cmd beside it to run on Windows."""
        out = _write_text(self, data, *args, **kw)
        if isinstance(data, str) and data.startswith("#!") and not self.suffix:
            program = sys.executable if "python" in data.splitlines()[0] else "bash"
            _write_text(self.with_name(self.name + ".cmd"),
                        f'@"{program}" "%~dp0{self.name}" %*\n')
        return out

    Path.write_text = write_text

    COLON = "fixture sessions like s-w1:p0 can't be file names on NTFS"
    LINE = "expects the POSIX line; test_orch_os and the live takeover cover the Windows one"
    SKIP = {
        "test_account_of[/opt/other-/opt/other]": "a POSIX path",
        "test_respawn_archives_into_the_lineage_and_relaunches_with_it": LINE,
        "test_a_queued_account_switch_applies_at_the_takeover": LINE,
        "test_account_switch_now_resumes_the_session_on_the_other_account": LINE,
        "test_new_starts_an_orchestrator_with_its_brief_log_and_run": LINE,
        "test_dry_run_prints_every_step_and_changes_nothing": LINE,
        "test_retry_on_another_account_restarts_claude_after_nobody_trusted_the_folder": LINE,
        "test_health_says_whether_a_start_can_work_here": "Windows looks for claude, not claude-acc",
        "test_pair_keeps_a_private_token_and_prints_the_pairing_string": "file modes",
        "test_pairing_stores_the_token_and_sends_a_test_push": "file modes",
        "test_health_says_how_full_the_machine_is": "no load average",
    }
    SKIP_PREFIX = {
        "test_a_machine_without_a_readable_meminfo_has_no_capacity": "no load average",
        "test_points_need_a_capture_from_before_the_window_opened": "no time.tzset",
        "test_report_lists_every_known_account_and_points_of_the_last_hour_and_today": "no time.tzset",
    }
    SKIP_COLON = {
        "test_usage_has_the_api_shape_and_points_per_period",
        "test_a_runs_hours_stop_at_its_end_and_max_workers_comes_from_its_settings",
        "test_a_running_runs_hours_count_to_now_not_its_end",
        "test_the_fleet_has_the_run_cost_from_the_background_pass_only",
        "test_a_run_whose_lead_spent_long_before_the_default_period_still_has_week_points",
        "test_a_state_push_carries_the_run_cost",
        "test_an_earlier_session_with_no_account_doesnt_hide_the_agents_account",
        "test_points_are_null_while_the_window_opened_before_the_capture_began",
        "test_points_come_back_with_the_first_window_opened_after_the_capture_began",
        "test_a_reading_with_no_capture_time_has_no_points",
    }

    def pytest_collection_modifyitems(items):
        for item in items:
            reason = SKIP.get(item.name) or (COLON if item.name in SKIP_COLON else None) or next(
                (why for start, why in SKIP_PREFIX.items() if item.name.startswith(start)), None)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))
