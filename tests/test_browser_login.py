import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from beauclaw import auth
from beauclaw.core import DEFAULT_COMPETITION


class BrowserError(Exception):
    pass


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock(url='https://competition.gitcode.com/competition/123/live-ranking')
        self.page.evaluate.return_value = 'synthetic-browser-token'
        self.context = MagicMock()
        self.context.pages = [self.page]
        self.context.cookies.return_value = []
        self.context.storage_state.return_value = {'origins': []}
        self.progress = MagicMock()

    def test_navigation_timeout_does_not_discard_a_valid_session(self):
        self.page.goto.side_effect = BrowserError('navigation timed out')
        with patch('beauclaw.auth.validate_auth') as validate:
            result = auth.wait_for_browser_auth(DEFAULT_COMPETITION, self.context, BrowserError, self.progress)
        self.assertEqual(result['token'], 'synthetic-browser-token')
        validate.assert_called_once_with(DEFAULT_COMPETITION, result)

    def test_redirect_race_recovers_from_persisted_origin_storage(self):
        self.page.evaluate.side_effect = BrowserError('execution context destroyed')
        self.context.storage_state.return_value = {'origins': [{'origin': 'https://competition.gitcode.com',
            'localStorage': [{'name': 'access_token', 'value': 'synthetic-persisted-token'}]}]}
        with patch('beauclaw.auth.validate_auth'):
            result = auth.wait_for_browser_auth(DEFAULT_COMPETITION, self.context, BrowserError, self.progress)
        self.assertEqual(result['token'], 'synthetic-persisted-token')

    def test_temporary_api_rejection_is_retried(self):
        clock = [100.0]
        def sleep(_):
            clock[0] += 10
        with patch('beauclaw.auth.time.monotonic', side_effect=lambda: clock[0]), \
             patch('beauclaw.auth.time.sleep', side_effect=sleep), \
             patch('beauclaw.auth.validate_auth', side_effect=[ValueError('not ready'), None]) as validate:
            result = auth.wait_for_browser_auth(DEFAULT_COMPETITION, self.context, BrowserError, self.progress)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result['token'], 'synthetic-browser-token')

    def test_foreign_origin_tokens_are_not_used(self):
        self.page.url = 'https://unrelated.example'
        self.context.storage_state.return_value = {'origins': [{'origin': 'https://unrelated.example',
            'localStorage': [{'name': 'access_token', 'value': 'synthetic-foreign-token'}]}]}
        with patch('beauclaw.auth.time.monotonic', side_effect=[0, 1, 3]), \
             patch('beauclaw.auth.time.sleep'), patch('beauclaw.auth.validate_auth') as validate:
            with self.assertRaisesRegex(ValueError, 'without verified leaderboard access'):
                auth.wait_for_browser_auth(DEFAULT_COMPETITION, self.context, BrowserError, self.progress, timeout=2)
        validate.assert_not_called()

    def test_cleanup_failure_does_not_overwrite_verified_login(self):
        manager = MagicMock()
        manager.__enter__.return_value.chromium.launch_persistent_context.return_value = self.context
        manager.__exit__.side_effect = BrowserError('driver cleanup failed')
        self.context.close.side_effect = BrowserError('browser already closed')
        module = types.ModuleType('playwright.sync_api')
        module.Error, module.sync_playwright = BrowserError, lambda: manager
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(sys.modules, {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': module}), \
             patch('beauclaw.auth.validate_auth'):
            path = Path(folder) / 'auth.json'
            auth.login(DEFAULT_COMPETITION, path, browser=True, profile=Path(folder) / 'profile')
            self.assertEqual(auth.load_auth(path)['token'], 'synthetic-browser-token')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
