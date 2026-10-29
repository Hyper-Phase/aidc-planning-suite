import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from web_report import build_report, validation
from test_api import api

class ReportAndSecrets(unittest.TestCase):
    def test_missing_evaluations_are_not_passes(self):
        checks = validation({}, {})
        self.assertFalse(any(r['executed'] for r in checks))
        self.assertEqual(checks[1]['status'], 'not_run')
        self.assertEqual(checks[2]['status'], 'not_run')

    def test_report_escapes_brief_and_handles_missing_sections(self):
        context = {'text': '<script>alert(1)</script>', 'research': {'point': {'name': '<img src=x onerror=alert(1)>'}}, 'facts': {}}
        report = build_report(context, {}, {'baseline': {}, 'version': 'v4.5'}, ({}, pd.DataFrame(), pd.DataFrame()), 'test-model')
        self.assertNotIn('<script>', report['html'])
        self.assertNotIn('<img src=x', report['html'])
        self.assertIn('&lt;script&gt;', report['html'])
        self.assertIn('not_run', report['html'])

    def test_file_and_environment_secret_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'openrouter.key'
            path.write_text('file-secret\n')
            with patch.dict(os.environ, {'OPENROUTER_API_KEY': '', 'OPENROUTER_API_KEY_FILE': str(path)}):
                self.assertEqual(api._server_key(), 'file-secret')
                self.assertNotIn('file-secret', str(api._configuration()))
                with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'env-secret'}):
                    self.assertEqual(api._server_key(), 'env-secret')
            with patch.dict(os.environ, {'OPENROUTER_API_KEY': '', 'OPENROUTER_API_KEY_FILE': str(path/'missing')}):
                self.assertFalse(api._configuration()['serverKeyConfigured'])
                with self.assertRaises(RuntimeError):
                    api._server_key()
