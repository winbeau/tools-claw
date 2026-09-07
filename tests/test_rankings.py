import contextlib
import json
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from beauclaw.auth import save_auth
from beauclaw.cli import main
from beauclaw.core import DEFAULT_COMPETITION, Store
from beauclaw.mail import create_message, deliver_one, load_mail_config, mail_policy, test_mail
from beauclaw.monitor import Collector
from beauclaw.rankings import Rankings
from test_beauclaw import payload, response, team


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'registry.sqlite3'
        self.config = self.root / 'mail.json'
        save_auth(self.config, {'provider': 'aliyun', 'sender': 'sender@example.com', 'password': 'synthetic-password'})
        self.store = Store(self.db)
        self.store.add_notice('first@example.com')
        self.store.add_notice('second@example.com')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, event_id=DEFAULT_COMPETITION, name='测试赛事_西北赛区'):
        with Rankings(self.db) as registry:
            return registry.add(event_id, name=name)

    def test_canonical_ranking_ids_and_retained_history_on_delete(self):
        first = self.add()
        self.assertRegex(first['short_id'], r'^[0-9a-f]{6}$')
        with Rankings(self.db) as registry:
            duplicate = registry.add(first['url'] + '?tab=region', name='ignored')
            self.assertEqual(duplicate['short_id'], first['short_id'])
            self.assertEqual(len(registry.list()), 1)
            with contextlib.closing(Store(Path(first['db']), DEFAULT_COMPETITION)) as history:
                history.record(response())
            registry.delete(first['short_id'])
            self.assertEqual(registry.list(), [])
            self.assertTrue(Path(first['db']).exists())
            restored = registry.add(first['url'])
            self.assertEqual(restored['short_id'], first['short_id'])
        with contextlib.closing(Store(Path(first['db']))) as history:
            self.assertEqual(history.summary()['poll_count'], 1)

    def test_legacy_recipient_and_ranking_migration(self):
        path = self.root / 'legacy.sqlite3'
        connection = sqlite3.connect(path)
        connection.executescript("CREATE TABLE notices(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE,created_at TEXT); INSERT INTO notices VALUES(1,'legacy@example.com','2026-01-01');")
        connection.close()
        with contextlib.closing(Store(path, DEFAULT_COMPETITION)) as history:
            history.record(response())
            code = history.notices()[0]['short_id']
            self.assertRegex(code, r'^[0-9a-f]{6}$')
        with Rankings(path) as registry:
            rows = registry.list()
            self.assertEqual(Path(rows[0]['db']), path)
            self.assertEqual(registry.store.summary()['poll_count'], 1)
            registry.store.delete_notice(code)
            self.assertEqual(registry.store.notices(), [])
            registry.delete(rows[0]['short_id'])
        with Rankings(path) as registry:
            self.assertEqual(registry.list(), [])  # Empty stays empty after migration.

    def test_independent_boards_shared_recipients_and_immutable_top_ten(self):
        policy = mail_policy(load_mail_config(self.config))
        first, second = self.add(), self.add('123456', '第二比赛_华东赛区')
        histories = [Store(Path(row['db']), row['competition_id'], notice_db=self.db) for row in (first, second)]
        try:
            original = [team(f'队伍{i}', str(100-i)) for i in range(12)]
            for history in histories:
                history.record(response(payload(original)), mail_policy=policy)
                self.assertEqual(history.summary()['mail_pending'], 0)
            changed = [team('新的榜首 <script>', '101.000000000000000001'), *original[:11]]
            histories[0].record(response(payload(changed)), mail_policy=policy)
            histories[1].record(response(payload([original[0], team('其他队', 30)])), mail_policy=policy)
            self.assertEqual(histories[0].summary()['mail_pending'], 2)
            self.assertEqual(histories[1].summary()['mail_pending'], 0)
            queued = json.loads(histories[0].db.execute('SELECT payload_json FROM mail_outbox ORDER BY id LIMIT 1').fetchone()[0])
            self.assertEqual(len(queued['top10']), 10)
            self.assertEqual(queued['top10'][0]['name'], '新的榜首 <script>')
            histories[0].record(response(payload([team('后来的榜首', 200)])), mail_policy=policy)
            message = create_message(queued, '<synthetic-message@example.com>')
            html = message.get_body(preferencelist=('html',)).get_content()
            self.assertEqual(message['From'].addresses[0].display_name, 'ICTHub')
            self.assertIn('&lt;script&gt;', html)
            self.assertNotIn('<script>', html)
            self.assertNotIn('后来的榜首', html)
            self.assertEqual(queued['top10'][0]['score'], '101.000000000000000001')
            self.assertIn('>101.000</td>', html)
            self.assertNotIn('101.000000000000000001', html)
            self.assertIn('分数 101.000\n', message.get_body(preferencelist=('plain',)).get_content())
            recipient = self.store.notices()[0]
            self.store.delete_notice(recipient['short_id'])
            with Rankings(self.db) as registry:
                registry.cancel_recipient(recipient['email'])
            self.assertEqual(histories[0].summary()['mail_pending'], 2)  # Two changes for the remaining recipient.
            with patch('beauclaw.mail.send_message') as send:
                self.assertTrue(deliver_one(histories[0], load_mail_config(self.config)))
                self.assertEqual(send.call_args.args[3], 'second@example.com')
        finally:
            for history in histories:
                history.close()

    def test_test_email_uses_first_ranking_top_ten_without_change_section(self):
        first = self.add()
        self.add('22222', '第二个比赛')
        with patch('beauclaw.mail.fetch', return_value=response(payload([team(f'队{i}', 100-i) for i in range(14)]))) as fetch, \
             patch('beauclaw.mail.send_message') as send:
            test_mail(self.config, self.store, auth_file=self.root / 'auth.json')
        self.assertEqual(fetch.call_args.args[0], first['competition_id'])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(send.call_count, 2)
        message = send.call_args_list[0].args[1]
        html = message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('队9', html)
        self.assertNotIn('队10', html)
        self.assertNotIn('变化前', html)
        self.assertNotIn('当前榜首', html)
        self.assertEqual(self.store.summary()['poll_count'], 0)

    def test_empty_rankings_prevent_test_network_and_mail(self):
        with patch('beauclaw.mail.fetch') as fetch, patch('beauclaw.mail.send_message') as send:
            with self.assertRaisesRegex(ValueError, 'Rankings is empty'):
                test_mail(self.root / 'missing-mail-config', self.store)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_targeted_test_cli_sends_once_without_modifying_recipients(self):
        first = self.add()
        self.add('22222', '第二个比赛')
        original = self.store.notices()
        args = ['beauclaw', 'notice', 'test', 'Preview@Example.com', '--db', str(self.db),
                '--mail-config', str(self.config), '--auth-file', str(self.root/'auth.json')]
        with patch('sys.argv', args), contextlib.redirect_stdout(io.StringIO()), \
             patch('beauclaw.mail.fetch', return_value=response(payload([team(f'队{i}', 100-i) for i in range(14)]))) as fetch, \
             patch('beauclaw.mail.send_message') as send:
            self.assertEqual(main(), 0)
        self.assertEqual(fetch.call_args.args[0], first['competition_id'])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[3], 'preview@example.com')
        message = send.call_args.args[1]
        self.assertEqual(str(message['To']), 'preview@example.com')
        self.assertIsNone(message['Importance'])
        html = message.get_body(preferencelist=('html',)).get_content()
        self.assertIn('队9', html)
        self.assertNotIn('队10', html)
        self.assertNotIn('变化前', html)
        self.assertEqual(self.store.notices(), original)
        self.assertEqual(self.store.summary()['poll_count'], 0)

    def test_targeted_test_works_with_an_empty_recipient_list(self):
        self.add()
        for notice in self.store.notices():
            self.store.delete_notice(notice['short_id'])
        with patch('beauclaw.mail.fetch', return_value=response()), patch('beauclaw.mail.send_message') as send:
            test_mail(self.config, self.store, auth_file=self.root/'auth.json', recipient='only@example.com')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.store.notices(), [])

    def test_targeted_test_rejects_invalid_address_or_empty_rankings_without_network(self):
        with patch('beauclaw.mail.fetch') as fetch, patch('beauclaw.mail.send_message') as send:
            with self.assertRaisesRegex(ValueError, 'complete email address'):
                test_mail(self.config, self.store, recipient='bad-address')
            with self.assertRaisesRegex(ValueError, 'Rankings is empty'):
                test_mail(self.config, self.store, recipient='only@example.com')
        fetch.assert_not_called()
        send.assert_not_called()

    def test_deleting_and_readding_a_ranking_does_not_send_stale_jobs(self):
        row = self.add()
        config = load_mail_config(self.config)
        policy = {**mail_policy(config), 'ranking_id': row['short_id'], 'ranking_generation': row['generation']}
        with contextlib.closing(Store(Path(row['db']), row['competition_id'], notice_db=self.db)) as history:
            history.record(response(), mail_policy=policy)
            history.record(response(payload([team('新榜首', 100)])), mail_policy=policy)
            with Rankings(self.db) as registry:
                registry.delete(row['short_id'])
                registry.add(row['url'])
            # Simulate a fetch committing its old subscription's job just after deletion.
            with history.db:
                history.db.execute('UPDATE mail_outbox SET cancelled_at=NULL')
            with patch('beauclaw.mail.send_message') as send:
                self.assertTrue(deliver_one(history, config))
                self.assertTrue(deliver_one(history, config))
                self.assertFalse(deliver_one(history, config))
            send.assert_not_called()
            self.assertEqual(history.summary()['mail_pending'], 0)

    def test_collectors_do_not_block_each_other(self):
        first, second = self.add(), self.add('12345', '第二个比赛')
        entered, release = threading.Event(), threading.Event()
        def fetch(event_id, *_):
            if event_id == first['competition_id']:
                entered.set()
                release.wait(5)
            return response()
        args = SimpleNamespace(db=self.db, interval=10, timeout=1, missing_samples=2, no_mail=True,
                               mail_config=self.config, auth_file=self.root/'auth', token_file=None,
                               once=True, samples=0)
        workers = [Collector(row, args) for row in (first, second)]
        with patch('beauclaw.monitor.fetch', side_effect=fetch):
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(entered.wait(2))
                workers[1].join(3)
                self.assertFalse(workers[1].is_alive())
                self.assertTrue(workers[0].is_alive())
                with contextlib.closing(Store(Path(second['db']))) as history:
                    self.assertEqual(history.summary()['poll_count'], 1)
            finally:
                release.set()
                for worker in workers:
                    worker.stop_event.set()
                    worker.join(5)


if __name__ == '__main__':
    unittest.main()
