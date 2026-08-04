# -*- coding: utf-8 -*-
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service as service


class CodexRetryLoggingTests(unittest.TestCase):
    def setUp(self):
        self.email = "retry@example.com"
        self.key = self.email.lower()
        with service._RETRYING_LOCK:
            service._clear_state_locked(self.key)
            service._STOP_REQUESTED.discard(self.key)

    def tearDown(self):
        with service._RETRYING_LOCK:
            service._clear_state_locked(self.key)
            service._STOP_REQUESTED.discard(self.key)

    def test_thread_filter_uses_id_instead_of_reused_thread_name(self):
        expected = logging.LogRecord("test", logging.INFO, __file__, 1, "ok", (), None)
        other = logging.LogRecord("test", logging.INFO, __file__, 1, "other", (), None)
        expected.thread = 101
        other.thread = 202
        expected.threadName = other.threadName = "codex-retry-retry@example.com"
        log_filter = service._ThreadIdFilter(101)

        self.assertTrue(log_filter.filter(expected))
        self.assertFalse(log_filter.filter(other))

    def test_removes_existing_handler_for_same_retry_log(self):
        logger = logging.Logger("codex-retry-test")
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "retry.log"
            handler = logging.FileHandler(path, encoding="utf-8")
            handler._codex_retry_log_path = str(path.resolve())
            logger.addHandler(handler)

            removed = service._remove_retry_log_handlers(logger, path)

        self.assertEqual(removed, 1)
        self.assertNotIn(handler, logger.handlers)

    def test_reserve_rejects_new_retry_while_stopped_thread_is_alive(self):
        with service._RETRYING_LOCK:
            service._RETRYING.add(self.key)
            service._RUNNING_THREADS[self.key] = 101
            service._RESERVED_AT[self.key] = 1
            service._STOP_REQUESTED.add(self.key)
        with patch.object(service, "_thread_alive", return_value=True), patch.object(
            service.db, "get_account_by_email", return_value={"codex_status": "stopped"}
        ), patch.object(service.time, "time", return_value=100):
            reserved = service.reserve(self.email)

        self.assertFalse(reserved)
        self.assertTrue(service.is_retrying(self.email))

    def test_reserve_clears_terminal_state_after_thread_exits(self):
        with service._RETRYING_LOCK:
            service._RETRYING.add(self.key)
            service._RUNNING_THREADS[self.key] = 101
            service._RESERVED_AT[self.key] = 1
            service._STOP_REQUESTED.add(self.key)
        with patch.object(service, "_thread_alive", return_value=False), patch.object(
            service.db, "get_account_by_email", return_value={"codex_status": "stopped"}
        ), patch.object(service.time, "time", return_value=100):
            reserved = service.reserve(self.email)

        self.assertTrue(reserved)
        self.assertFalse(service.is_stop_requested(self.email))


if __name__ == "__main__":
    unittest.main()
