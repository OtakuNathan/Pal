from __future__ import annotations

import unittest
from pathlib import Path

from pal.foundation.service_logging import (
    PAL_SERVICE_LOG_SINK_ENV,
    PAL_SERVICE_LOG_TAG_ENV,
    service_log_plan,
)


class TestServiceLogging(unittest.TestCase):
    def test_linux_service_log_plan_uses_systemd_journal(self) -> None:
        plan = service_log_plan(Path("/runtime"), service_name="pal.service", platform_name="Linux")

        self.assertEqual(plan.kind, "systemd_journal")
        self.assertEqual(plan.environment()[PAL_SERVICE_LOG_SINK_ENV], "systemd_journal")
        self.assertEqual(plan.environment()[PAL_SERVICE_LOG_TAG_ENV], "pal.service")
        self.assertEqual(plan.journal_command, "journalctl --user -u pal.service -f")

    def test_macos_service_log_plan_uses_unified_logging(self) -> None:
        plan = service_log_plan(Path("/runtime"), service_name="com.pal.runtime", platform_name="Darwin")

        self.assertEqual(plan.kind, "macos_unified")
        self.assertEqual(plan.environment()[PAL_SERVICE_LOG_SINK_ENV], "macos_unified")
        self.assertEqual(plan.environment()[PAL_SERVICE_LOG_TAG_ENV], "com.pal.runtime")
        self.assertIn("log stream", plan.stream_command)
        self.assertIn("[com.pal.runtime]", plan.stream_command)


if __name__ == "__main__":
    unittest.main()
