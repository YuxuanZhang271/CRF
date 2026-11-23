"""Diagnostic capture, tracebacks, cursor bounds and HTTP log echo prevention."""

import logging
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from diagnostics import DiagnosticLog, RunLog


class DiagnosticTests(unittest.TestCase):
    def test_run_logs_only_warnings_errors_tracebacks_and_unique_start_times(self):
        logger = logging.getLogger('run-log-test')
        with tempfile.TemporaryDirectory() as directory:
            original_hook = threading.excepthook
            with RunLog(directory) as first:
                logger.warning('A run warning: 嗰個係咩？')
                logger.error('A run error')
                logger.info('Ordinary info must not enter the warning log')
                warnings.warn('Library warning for this run', UserWarning)
                try:
                    raise ValueError('Failure with traceback')
                except ValueError:
                    logger.exception('Detailed failure')
            with RunLog(directory) as second:
                logger.warning('Second run only')
            self.assertNotEqual(first.path, second.path)
            text = first.path.read_text(encoding='utf-8')
            self.assertIn('Run started:', text)
            self.assertIn('Run ended:', text)
            self.assertIn('嗰個係咩？', text)
            self.assertIn('Library warning for this run', text)
            self.assertIn('ValueError: Failure with traceback', text)
            self.assertNotIn('Ordinary info', text)
            self.assertNotIn('Second run only', text)
            self.assertIs(threading.excepthook, original_hook)

    def test_tracebacks_cursor_and_bounded_history(self):
        feed = DiagnosticLog(capacity=2)
        try:
            try:
                raise ValueError('bad tokenizer input')
            except ValueError:
                record = logging.LogRecord('LLM', logging.ERROR, __file__, 1,
                                           'Command failed', (), sys.exc_info())
            feed.emit(record)
            first = feed.snapshot()
            self.assertIn('ValueError: bad tokenizer input', first['messages'][0]['message'])
            self.assertEqual(first['messages'][0]['level'], 'error')
            for message in ('Running', 'Ready'):
                feed.emit(logging.LogRecord('Tasks', logging.INFO, __file__, 1, message, (), None))
            latest = feed.snapshot(after=1)
            self.assertEqual(len(latest['messages']), 2)
            self.assertEqual(latest['sequence'], 3)
            self.assertEqual(latest['messages'][0]['source'], 'TASK')
            self.assertEqual(feed.snapshot(after=3)['messages'], [])
            echoed = logging.LogRecord('WindowsApp', logging.INFO, __file__, 1, 'Already in browser', (), None)
            echoed.from_dashboard = True
            feed.emit(echoed)
            self.assertEqual(feed.snapshot()['sequence'], 3)
        finally:
            feed.close()


if __name__ == '__main__':
    unittest.main()
