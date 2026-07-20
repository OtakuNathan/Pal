from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

from pal.foundation.service_logging import (
    PAL_SERVICE_LOG_SINK_ENV,
    PAL_SERVICE_LOG_TAG_ENV,
    _RedactingFormatter,
    redact_service_log_text,
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

    def test_service_log_redaction_removes_credentials_from_urls_and_headers(self) -> None:
        secret = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFG"
        rendered = redact_service_log_text(
            "POST https://api.telegram.org/bot"
            + secret
            + "/sendMessage?access_token=private-access-token Authorization: Bearer private-bearer-token"
        )

        self.assertNotIn(secret, rendered)
        self.assertNotIn("private-access-token", rendered)
        self.assertNotIn("private-bearer-token", rendered)
        self.assertIn("api.telegram.org/bot<redacted>/sendMessage", rendered)

    def test_service_log_formatter_redacts_exception_traceback(self) -> None:
        secret = "123456789:abcdefghijklmnopqrstuvwxyzABCDEFG"
        try:
            raise RuntimeError(f"request failed at https://api.telegram.org/bot{secret}/getUpdates")
        except RuntimeError:
            record = logging.LogRecord(
                name="pal.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="channel request failed",
                args=(),
                exc_info=sys.exc_info(),
            )

        rendered = _RedactingFormatter("%(levelname)s %(message)s").format(record)
        self.assertNotIn(secret, rendered)
        self.assertIn("bot<redacted>/getUpdates", rendered)


if __name__ == "__main__":
    unittest.main()
