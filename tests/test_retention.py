import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from beauclaw.core import DEFAULT_COMPETITION, Store, dumps
from beauclaw.mail import create_message, deliver_one
from test_beauclaw import payload, response, team


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'history.sqlite3'
        self.store = Store(self.path, DEFAULT_COMPETITION)
        self.store.add_notice('recipient@example.com')
        self.policy = {'sender': 'sender@example.com', 'boards': ['realtime_region_ranking']}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def sample(self, name='甲队', score=10, **kwargs):
        return self.store.record(response(payload([team(name, score)])), **kwargs)[0]

    def test_ordinary_limit_body_cleanup_and_preserved_history_across_restart(self):
        first = response(payload([team('榜首', 1000), team('其他队', 500)]))
        self.store.record(first)
        for i in range(134):
            self.store.record(response(payload([team('榜首', 1000), team('其他队', 400-i)])))
        summary = self.store.summary()
        self.assertEqual(summary['poll_count'], 100)
        self.assertEqual(summary['critical_poll_count'], 0)
        self.assertIsNone(self.store.snapshot(1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM bodies').fetchone()[0], 100)
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM bodies WHERE sha256=?', (hashlib.sha256(first.body).hexdigest(),)).fetchone())
        events = self.store.events(limit=1000)
        self.assertTrue(any(event['poll_id'] == 2 and not event['snapshot_available'] for event in events))
        self.assertEqual(summary['states'][0]['members']['name:其他队']['peak_score'], '500')
        self.store.close()
        self.store = Store(self.path, DEFAULT_COMPETITION)
        poll, _ = self.store.record(response(payload([team('榜首', 1000), team('其他队', 100)])))
        self.assertEqual(poll['id'], 136)
        self.assertEqual(self.store.summary()['poll_count'], 100)
        self.assertEqual(self.store.summary()['critical_poll_count'], 0)

    def test_both_directions_are_critical_but_only_rises_or_replacement_email(self):
        self.sample(mail_policy=self.policy)
        dropped = self.sample(score='2.000000000000000000000001', mail_policy=self.policy)
        self.assertTrue(dropped['critical'])
        self.assertEqual(self.store.summary()['mail_pending'], 0)
        raised = self.sample(score='2.000000000000000000000002', mail_policy=self.policy)
        replaced = self.sample(name='乙队', score=1, mail_policy=self.policy)
        self.sample(name='乙队', score='1.000', mail_policy=self.policy)
        rows = self.store.db.execute('SELECT * FROM mail_outbox ORDER BY id').fetchall()
        self.assertEqual([row['poll_id'] for row in rows], [raised['id'], replaced['id']])
        for poll_id in range(1, 5):
            self.assertTrue(self.store.snapshot(poll_id)['critical'])
        for row in rows:
            message = create_message(json.loads(row['payload_json']), row['message_id'])
            self.assertIn('关键邮件', str(message['Subject']))
            self.assertEqual(message['Importance'], 'high')
            self.assertEqual(message['X-Priority'], '1')

    def test_more_than_one_hundred_critical_captures_and_shared_bodies_survive(self):
        for score in range(1000, 869, -1):
            self.sample(score=score)
        for _ in range(120):
            self.sample(score=870)
        summary = self.store.summary()
        self.assertEqual(summary['critical_poll_count'], 131)
        self.assertEqual(summary['poll_count'], 231)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM bodies').fetchone()[0], 131)
        for poll_id in range(1, 132):
            self.assertIsNotNone(self.store.snapshot(poll_id))
        self.assertIsNone(self.store.snapshot(132))
        self.assertIsNotNone(self.store.snapshot(251))
        for event in self.store.events(limit=1000):
            if event['kind'] == 'leader_changed':
                self.assertTrue(event['snapshot_available'])
                self.assertTrue(event['previous_snapshot_available'])

    def test_outage_does_not_discard_the_next_changes_comparison_evidence(self):
        baseline = self.sample()
        for _ in range(130):
            self.store.record(response({'error': 'synthetic outage'}, http_status=503))
        self.assertEqual(self.store.summary()['poll_count'], 100)
        self.assertIsNotNone(self.store.snapshot(baseline['id']))
        self.assertFalse(self.store.snapshot(baseline['id'])['critical'])
        changed = self.sample(score=9)
        self.assertEqual(changed['id'], 132)
        self.assertTrue(self.store.snapshot(baseline['id'])['critical'])
        self.assertTrue(self.store.snapshot(changed['id'])['critical'])

    def test_historical_schedule_baseline_is_preserved_without_false_change_mail(self):
        baseline = self.sample()
        for _ in range(130):
            self.store.record(response(payload([team('决赛榜首', 5)], schedule='final')), mail_policy=self.policy)
        self.assertEqual(self.store.summary()['poll_count'], 100)
        self.assertEqual(self.store.summary()['critical_poll_count'], 0)
        self.assertEqual(self.store.summary()['mail_pending'], 0)
        self.sample(score=9, mail_policy=self.policy)
        self.assertTrue(self.store.snapshot(baseline['id'])['critical'])
        self.assertEqual(self.store.summary()['mail_pending'], 0)

    def test_legacy_migration_preserves_changes_and_all_mail_states(self):
        # Construct an authentic pre-retention schema, including old captures that
        # would fall outside the ordinary window on the first upgraded sample.
        self.sample(mail_policy=self.policy)
        self.sample(score=11, mail_policy=self.policy)
        with patch.object(Store, '_prune_snapshots'):
            for i in range(140):
                self.sample(score=11, mail_policy=self.policy)
        with self.store.db:
            self.store.db.execute("UPDATE mail_outbox SET sent_at='2026-09-06'")
        mail_before = [tuple(row) for row in self.store.db.execute('SELECT * FROM mail_outbox')]
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                DROP INDEX polls_critical_id;
                ALTER TABLE polls DROP COLUMN critical;
                ALTER TABLE polls DROP COLUMN critical_reason;
                DELETE FROM meta WHERE key='snapshot_retention_v1';
            """)
        self.store = Store(self.path, DEFAULT_COMPETITION)
        self.assertTrue(self.store.snapshot(1)['critical'])
        self.assertEqual(self.store.snapshot(2)['critical_reason'], 'leader_changed')
        self.sample(score=11, mail_policy=self.policy)
        self.assertEqual(self.store.summary()['poll_count'], 102)
        self.assertEqual(mail_before, [tuple(row) for row in self.store.db.execute('SELECT * FROM mail_outbox')])
        self.assertTrue(self.store.snapshot(1)['critical'])
        self.assertTrue(self.store.snapshot(2)['critical'])

    def test_old_queued_drop_is_kept_but_not_sent_after_upgrade(self):
        self.sample(mail_policy=self.policy)
        self.sample(score=9, mail_policy=self.policy)
        event = next(e for e in self.store.events() if e['kind'] == 'leader_changed' and e['scope'].endswith(':realtime_region_ranking'))
        old = {'events': [event], 'recipient': 'recipient@example.com', 'sender': self.policy['sender']}
        with self.store.db:
            self.store.db.execute('INSERT INTO mail_outbox(poll_id,recipient,message_id,payload_json) VALUES (?,?,?,?)',
                                  (2, old['recipient'], '<old-drop@example.com>', dumps(old)))
        with patch('beauclaw.mail.send_message') as send:
            self.assertTrue(deliver_one(self.store, {}))
        send.assert_not_called()
        row = self.store.db.execute('SELECT * FROM mail_outbox').fetchone()
        self.assertIsNotNone(row['cancelled_at'])
        self.assertIsNone(row['sent_at'])
        self.assertEqual(row['attempts'], 0)
        for _ in range(130):
            self.sample(score=9)
        self.assertTrue(self.store.snapshot(2)['critical'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM mail_outbox').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
