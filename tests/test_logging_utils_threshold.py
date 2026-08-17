"""Tests for ThresholdExceptionFormatter (src/utils/logging_utils.py).

Covers the formatter in isolation (traceback suppressed below threshold,
rendered at/above threshold, record left intact for downstream handlers)
plus an integration check proving DiagnosticsLogHandler still captures a
traceback for a WARNING logged with ``exc_info=True`` even when a
ThresholdExceptionFormatter-equipped StreamHandler processes the same
LogRecord first.
"""
import io
import logging
import os
import sys
import tempfile
import unittest

from src.services.diagnostics import DiagnosticsLogHandler
from src.utils.logging_utils import ThresholdExceptionFormatter


def _record_with_real_exc_info(
    level: int,
    msg: str,
    logger_name: str = 'test.threshold',
) -> logging.LogRecord:
    """Build a LogRecord carrying a genuine exc_info tuple from a raised exception."""
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


class TestThresholdExceptionFormatterUnit(unittest.TestCase):
    """Unit tests for ThresholdExceptionFormatter.format()."""

    def test_warning_with_exc_info_formats_without_traceback(self):
        formatter = ThresholdExceptionFormatter('%(levelname)s - %(message)s')
        record = _record_with_real_exc_info(logging.WARNING, "something odd happened")

        output = formatter.format(record)

        self.assertNotIn('Traceback', output)
        self.assertEqual(output, 'WARNING - something odd happened')

    def test_error_with_exc_info_formats_with_traceback(self):
        formatter = ThresholdExceptionFormatter('%(levelname)s - %(message)s')
        record = _record_with_real_exc_info(logging.ERROR, "something odd happened")

        output = formatter.format(record)

        self.assertIn('Traceback', output)
        self.assertIn('ValueError: boom', output)

    def test_exc_info_intact_after_suppressed_warning_format(self):
        formatter = ThresholdExceptionFormatter('%(levelname)s - %(message)s')
        record = _record_with_real_exc_info(logging.WARNING, "something odd happened")

        formatter.format(record)

        self.assertIsNotNone(record.exc_info)
        self.assertIsNone(record.exc_text)  # never computed/cached since suppressed

    def test_exc_info_intact_after_error_format(self):
        formatter = ThresholdExceptionFormatter('%(levelname)s - %(message)s')
        record = _record_with_real_exc_info(logging.ERROR, "something odd happened")

        formatter.format(record)

        self.assertIsNotNone(record.exc_info)

    def test_custom_threshold_lets_warning_render_traceback(self):
        formatter = ThresholdExceptionFormatter(
            '%(levelname)s - %(message)s', threshold=logging.WARNING
        )
        record = _record_with_real_exc_info(logging.WARNING, "something odd happened")

        output = formatter.format(record)

        self.assertIn('Traceback', output)

    def test_record_without_exc_info_is_unaffected(self):
        formatter = ThresholdExceptionFormatter('%(levelname)s - %(message)s')
        record = logging.LogRecord(
            name='test.threshold',
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg='plain message',
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)

        self.assertEqual(output, 'WARNING - plain message')


class TestThresholdFormatterDiagnosticsIntegration(unittest.TestCase):
    """A WARNING logged with exc_info=True inside an except block must still
    reach DiagnosticsLogHandler with its traceback intact, even when a
    ThresholdExceptionFormatter-equipped StreamHandler runs first on the same
    LogRecord and suppresses the traceback in its own rendered output.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['DIAGNOSTICS_OPT_IN'] = 'true'
        self._test_logger = logging.getLogger('test_threshold_diagnostics_integration')
        self._test_logger.propagate = False
        self._test_logger.setLevel(logging.DEBUG)
        self._stream = io.StringIO()

    def tearDown(self):
        self._test_logger.handlers.clear()
        os.environ.pop('DIAGNOSTICS_OPT_IN', None)
        self._tmp.cleanup()

    def test_diagnostics_handler_still_captures_traceback_behind_threshold_formatter(self):
        stream_handler = logging.StreamHandler(self._stream)
        stream_handler.setLevel(logging.WARNING)
        stream_handler.setFormatter(ThresholdExceptionFormatter('%(levelname)s - %(message)s'))

        diagnostics_handler = DiagnosticsLogHandler(data_dir=self._tmp.name)
        diagnostics_handler.setLevel(logging.INFO)

        # Attach in the same relative order production does: the console
        # handler is wired up at logging_utils import time, while the
        # diagnostics handler is only added later during app startup (see
        # web_server.py's setup_diagnostics_logging() call). So the
        # StreamHandler's format() runs first for any given record, and the
        # diagnostics handler's emit() runs second on that SAME record
        # object -- exactly the ordering that requires the formatter to
        # restore exc_info before returning.
        self._test_logger.addHandler(stream_handler)
        self._test_logger.addHandler(diagnostics_handler)

        try:
            raise ValueError("simulated failure")
        except ValueError:
            self._test_logger.warning("Sync step failed unexpectedly", exc_info=True)

        # Console-facing output must be a single line -- no traceback block.
        console_output = self._stream.getvalue()
        self.assertNotIn('Traceback', console_output)
        self.assertIn('Sync step failed unexpectedly', console_output)

        # The diagnostics handler, processing the same record right after
        # the StreamHandler, must still have captured the full traceback.
        with diagnostics_handler._lock:
            self.assertEqual(len(diagnostics_handler._entries), 1)
            entry = next(iter(diagnostics_handler._entries.values()))
            self.assertIn('traceback', entry)
            self.assertIn('ValueError', entry['traceback'])


if __name__ == '__main__':
    unittest.main()
