import hashlib
import json
import os
import stat
import smtplib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import beauclaw.cli as cli
from beauclaw.auth import load_auth, login, save_auth
from beauclaw.core import DEFAULT_COMPETITION, InvalidBoard, Response, Store, api_url, parse_board
from beauclaw.mail import MailWorker, configure_mail, create_message, deliver_one, load_mail_config, mail_policy, show_config

TIME = "2026-09-06T17:00:00.000+00:00"


def team(name, score, team_id=None):
    row = {"team_name": name, "score": score}
    if team_id is not None:
        row["team_id"] = team_id
    return row


def payload(rows=None, schedule="preliminary", region=None):
    rows = rows if rows is not None else [team("甲队", "10"), team("乙队", "9")]
    return {"current_schedule": {"id": schedule, "name": "初赛", "seal_time": "2026-10-17T18:00:00"},
            "realtime_all_member_ranking": rows,
            "realtime_region_ranking": rows if region is None else region}


def response(data=None, http_status=200):
    data = payload() if data is None else data
    body = json.dumps(data, ensure_ascii=False).encode()
    return Response(TIME, TIME, api_url(DEFAULT_COMPETITION), http_status, body)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.store = Store(self.path, DEFAULT_COMPETITION)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def record(self, data=None, http_status=200):
        return self.store.record(response(data, http_status))

    def state(self):
        return next(s for s in self.store.summary()["states"] if s["board"] == "realtime_all_member_ranking")

    def test_baseline_repeated_body_dedup_and_exact_evidence(self):
        first, events = self.record()
        self.assertEqual([e["kind"] for e in events], ["baseline", "baseline"])
        second, events = self.record()
        self.assertEqual(events, [])
        self.assertEqual(self.store.summary()["poll_count"], 2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM bodies").fetchone()[0], 1)
        saved = self.store.snapshot(second["id"])
        self.assertEqual(saved["raw_body"].encode(), response().body)
        self.assertEqual(saved["body_sha256"], hashlib.sha256(response().body).hexdigest())
        self.assertNotEqual(first["id"], second["id"])

    def test_drop_retains_peak_and_recovery_is_not_new_team(self):
        self.record()
        _, events = self.record(payload([team("甲队", "2"), team("乙队", "9")]))
        self.assertEqual(sum(e["kind"] == "score_drop" for e in events), 2)
        self.assertEqual(self.state()["members"]["name:甲队"]["peak_score"], "10")
        self.record(payload([team("甲队", "12"), team("乙队", "9")]))
        self.assertEqual(self.state()["members"]["name:甲队"]["peak_score"], "12")

    def test_missing_returning_and_restart(self):
        self.record()
        _, events = self.record(payload([team("乙队", "9")]))
        self.assertIn("missing", [e["kind"] for e in events])
        self.assertNotIn("missing_confirmed", [e["kind"] for e in events])
        self.store.close()
        self.store = Store(self.path, DEFAULT_COMPETITION)
        _, events = self.record(payload([team("乙队", "9")]))
        confirmed = [e for e in events if e["kind"] == "missing_confirmed"]
        self.assertEqual(len(confirmed), 2)
        self.assertEqual(confirmed[0]["details"]["last_seen_poll_id"], 1)
        _, events = self.record(payload([team("乙队", "9")]))
        self.assertNotIn("missing_confirmed", [e["kind"] for e in events])
        _, events = self.record()
        self.assertEqual(sum(e["kind"] == "returned" for e in events), 2)
        self.assertTrue(self.state()["members"]["name:甲队"]["present"])

    def test_first_appearance_and_stable_id_rename(self):
        self.record(payload([team("原队名", 10, "large-id")]))
        _, events = self.record(payload([team("新队名", 10, "large-id"), team("新队", 9, "new")]))
        self.assertEqual(sum(e["kind"] == "renamed" for e in events), 2)
        self.assertEqual(sum(e["kind"] == "appeared" for e in events), 2)
        self.assertNotIn("missing", [e["kind"] for e in events])

    def test_name_only_rename_is_not_claimed_to_be_same_identity(self):
        self.record(payload([team("原队名", 10), team("乙队", 9)]))
        _, events = self.record(payload([team("新队名", 10), team("乙队", 9)]))
        self.assertIn("appeared", [e["kind"] for e in events])
        self.assertIn("missing", [e["kind"] for e in events])
        self.assertNotIn("renamed", [e["kind"] for e in events])

    def test_errors_and_empty_boards_do_not_delete_or_advance_missing(self):
        self.record()
        initial = self.state()["members"]
        for code in (401, 403, 418, 429, 500):
            poll, events = self.record({"error_code": code}, code)
            self.assertEqual(poll["status"], "error")
            self.assertEqual(events, [])
        for data in ({"error_code": 401}, {"data": None}, [], {"current_schedule": None}, payload([])):
            self.record(data)
        self.assertEqual(self.state()["members"], initial)
        poll, events = self.record(payload(region=[]))
        self.assertEqual(poll["status"], "ok")
        self.assertEqual([e["kind"] for e in events], ["board_empty"])
        self.assertTrue(all(m["present"] for m in self.state()["members"].values()))

    def test_schedule_switch_and_seal_time_do_not_report_mass_disappearance(self):
        self.record()
        _, events = self.record(payload([team("新赛程队", 5)], schedule="final"))
        self.assertEqual([e["kind"] for e in events], ["baseline", "baseline"])
        self.assertEqual(len(self.store.summary()["states"]), 4)
        data = payload([])
        data["current_schedule"]["seal_time"] = "2026-09-07T00:30:00"  # 16:30 UTC
        poll, events = self.record(data)
        self.assertEqual(poll["status"], "sealed")
        self.assertEqual(events, [])

    def test_schema_validation_rejects_partial_snapshot(self):
        self.record()
        invalid = payload()
        del invalid["realtime_region_ranking"]
        for data in (invalid, payload([team("甲队", 10), team("甲队", 9)]), payload([team("甲队", None)])):
            poll, events = self.record(data)
            self.assertEqual(poll["status"], "error")
            self.assertEqual(events, [])
        self.assertEqual(len(self.state()["members"]), 2)

    def test_decimal_precision_large_ids_and_rank_reordering(self):
        a = team("甲", "1.000000000000000000000000000001", "2094722369343447042")
        b = team("乙", "1", "2094722369343447043")
        self.record(payload([a, b]))
        a["score"] = "1.000000000000000000000000000002"
        _, events = self.record(payload([b, a]))
        self.assertEqual(sum(e["kind"] == "score_up" for e in events), 2)
        self.assertNotIn("appeared", [e["kind"] for e in events])
        self.assertIn("team_id:2094722369343447042", self.state()["members"])

    def test_wrong_competition_cannot_mix_into_database(self):
        with self.assertRaises(ValueError):
            Store(self.path, "123")

    def test_signal_filter_pagination_and_csv(self):
        self.record()
        self.record(payload([team("=danger()", "9"), team("乙队", "1")]))
        signals = self.store.events(signals_only=True)
        self.assertTrue(all(e["kind"] in ("missing", "score_drop", "leader_changed") for e in signals))
        page1 = self.store.events(limit=2)
        page2 = self.store.events(limit=2, before_id=page1[-1]["id"])
        self.assertLess(page2[0]["id"], page1[-1]["id"])
        output = Path(self.directory.name) / "events.csv"
        cli.export_events(self.store, output)
        self.assertIn("'=danger()", output.read_text(encoding="utf-8-sig"))

    def test_local_http_dashboard_and_snapshot_routes(self):
        self.record()
        server = cli.server_for(self.path, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        urlopen = build_opener(ProxyHandler({})).open
        try:
            with urlopen(base + "/api/summary") as r:
                self.assertEqual(json.load(r)["poll_count"], 1)
            with urlopen(base + "/api/snapshots/1") as r:
                self.assertIn("raw_body", json.load(r))
            with urlopen(base) as r:
                self.assertIn("榜单留痕", r.read().decode())
                self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base, headers={"Host": "unrelated.example"}))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/api/snapshots/nope")
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class AuthAndRetryTests(unittest.TestCase):
    def test_auth_save_permissions_and_failed_login_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / ".auth" / "auth.json"
            save_auth(path, {"token": "existing-test-credential"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_auth(path)["token"], "existing-test-credential")
            token_file = Path(folder) / "token"
            token_file.write_text("invalid-test-credential")
            with patch("beauclaw.auth.fetch", return_value=response({"error_code": 425}, 400)):
                with self.assertRaises(ValueError):
                    login(DEFAULT_COMPETITION, path, token_file=token_file, profile=Path(folder) / "browser")
            self.assertEqual(load_auth(path)["token"], "existing-test-credential")

    def test_retry_after_and_auth_backoff(self):
        r = response(http_status=429)
        r.headers["retry-after"] = "120"
        self.assertEqual(cli.retry_delay(r, 1, 10), 120)
        self.assertEqual(cli.retry_delay(response(http_status=401), 1, 10), 60)
        self.assertEqual(cli.retry_delay(response(http_status=500), 8, 10), 300)
        self.assertEqual(cli.retry_delay(response(), 0, 10), 10)

    def test_html_and_business_errors_are_not_rankings(self):
        for body in (b"<html>login</html>", b'{"code":401,"data":{}}'):
            with self.assertRaises(InvalidBoard):
                parse_board(body, TIME)

    def test_request_duration_is_included_in_ten_second_interval(self):
        clock = {"now": 0.0}
        starts, waits = [], []

        class Stop:
            def is_set(self):
                return False

            def set(self):
                pass

            def wait(self, duration):
                waits.append(duration)
                clock["now"] += duration

        def sample(*args):
            starts.append(clock["now"])
            clock["now"] += 2  # Simulated two-second HTTP request.
            return response()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = SimpleNamespace(db=root / "db", competition=DEFAULT_COMPETITION, interval=10,
                                   timeout=8, missing_samples=2, no_web=True, no_mail=True, once=False,
                                   samples=3, auth_file=root / "auth", token_file=None)
            with patch("beauclaw.cli.threading.Event", return_value=Stop()), \
                 patch("beauclaw.cli.time.monotonic", side_effect=lambda: clock["now"]), \
                 patch("beauclaw.cli.fetch", side_effect=sample):
                self.assertEqual(cli.watch(args), 0)
        self.assertEqual(starts, [0, 10, 20])
        self.assertEqual(waits, [8, 8])


class MailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.store = Store(self.path, DEFAULT_COMPETITION)
        self.config_path = Path(self.directory.name) / "smtp.json"
        self.store.add_notice("notify@example.com")
        save_auth(self.config_path, {"provider": "aliyun", "sender": "sender@example.com", "password": "test-smtp-secret"})
        with patch.dict(os.environ, {}, clear=True):
            self.config = load_mail_config(self.config_path)
        self.policy = mail_policy(self.config)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def record(self, data=None):
        return self.store.record(response(data), mail_policy=self.policy)

    def queue(self):
        return self.store.db.execute("SELECT * FROM mail_outbox ORDER BY id").fetchall()

    def test_default_region_only_and_no_initial_or_unchanged_mail(self):
        self.assertEqual(self.config["host"], "smtpdm.aliyun.com")
        self.assertEqual(self.config["port"], 465)
        self.assertEqual(self.config["boards"], ["realtime_region_ranking"])
        self.record()
        self.record()
        # The all-member leader changes while the region leader stays the same.
        self.record(payload([team("总榜新榜首", 99)], region=payload()["realtime_region_ranking"]))
        self.assertEqual(len(self.queue()), 0)
        # Only regional #1 score changes; one durable message contains that board only.
        self.record(payload([team("总榜新榜首", 99)], region=[team("甲队", 11), team("乙队", 9)]))
        self.record(payload([team("总榜新榜首", 99)], region=[team("甲队", "11.00"), team("乙队", 9)]))
        rows = self.queue()
        self.assertEqual(len(rows), 1)
        data = json.loads(rows[0]["payload_json"])
        self.assertEqual(len(data["events"]), 1)
        self.assertNotIn("password", data)
        message = create_message(data, rows[0]["message_id"])
        text = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("甲队", text)
        self.assertIn("分数 10", text)
        self.assertIn("分数 11", text)
        self.assertIn("2026-09-07 01:00:00", text)
        self.assertIn("北京时间", text)
        self.assertIn("参赛区域实时总榜", text)

    def test_leader_replacement_and_lower_rank_score_changes(self):
        self.record()
        self.record(payload([team("甲队", 10), team("乙队", 8)]))
        self.assertEqual(len(self.queue()), 0)
        self.record(payload([team("乙队", 11), team("甲队", 10)]))
        self.assertEqual(len(self.queue()), 1)
        event = json.loads(self.queue()[0]["payload_json"])["events"][0]
        self.assertEqual(event["before"]["name"], "甲队")
        self.assertEqual(event["after"]["name"], "乙队")

    def test_empty_region_and_new_schedule_do_not_notify(self):
        self.record()
        self.record(payload(region=[]))
        self.record(payload([team("新赛程队", 100)], schedule="final"))
        self.assertEqual(len(self.queue()), 0)

    def test_retry_persists_across_restart_and_accepted_mail_is_not_resent(self):
        self.record()
        self.record(payload([team("甲队", 12), team("乙队", 9)]))
        self.store.close()
        self.store = Store(self.path, DEFAULT_COMPETITION)
        message_id = self.queue()[0]["message_id"]
        with patch("beauclaw.mail.send_message", side_effect=smtplib.SMTPException("test-smtp-secret")):
            self.assertTrue(deliver_one(self.store, self.config))
        row = self.queue()[0]
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["attempts"], 1)
        self.assertNotIn("secret", row["last_error"])
        self.assertFalse(deliver_one(self.store, self.config))  # Not due yet.
        with self.store.db:
            self.store.db.execute("UPDATE mail_outbox SET next_attempt=0")
        with patch("beauclaw.mail.send_message") as send:
            self.assertTrue(deliver_one(self.store, self.config))
            self.assertEqual(send.call_args.args[1]["Message-ID"], message_id)
            self.assertFalse(deliver_one(self.store, self.config))
            send.assert_called_once()
        self.assertIsNotNone(self.queue()[0]["sent_at"])
        self.assertEqual(self.store.summary()["mail_pending"], 0)
        self.assertEqual(self.store.summary()["mail_sent"], 1)

    def test_slow_smtp_does_not_block_snapshot_writes(self):
        self.record()
        self.record(payload([team("甲队", 12), team("乙队", 9)]))
        entered, release = threading.Event(), threading.Event()

        def slow_send(*args):
            entered.set()
            release.wait(5)

        with patch("beauclaw.mail.send_message", side_effect=slow_send):
            worker = MailWorker(self.path, self.config_path)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                # SMTP is still blocked, but polling can commit its next snapshot.
                self.record(payload([team("甲队", 13), team("乙队", 9)]))
                self.assertFalse(release.is_set())
                self.assertEqual(self.store.summary()["poll_count"], 3)
            finally:
                release.set()
                worker.close()

    def test_multiple_recipients_are_queued_independently_and_removed_ones_cancelled(self):
        self.assertTrue(self.store.add_notice("second@example.com"))
        self.assertFalse(self.store.add_notice("SECOND@example.com"))
        self.record()
        self.record(payload([team("甲队", 12), team("乙队", 9)]))
        self.assertEqual(len(self.queue()), 2)
        first_id = self.store.notices()[0]["id"]
        self.store.delete_notice(str(first_id))
        self.assertEqual(self.store.summary()["mail_pending"], 1)
        with patch("beauclaw.mail.send_message") as send:
            self.assertTrue(deliver_one(self.store, self.config))
            self.assertEqual(send.call_args.args[3], "second@example.com")
            self.assertFalse(deliver_one(self.store, self.config))
        self.assertEqual(self.store.notices()[0]["email"], "second@example.com")
        self.store.delete_notice("second@example.com")
        self.assertEqual(self.store.notices(), [])

    def test_failed_recipient_does_not_block_others(self):
        self.store.add_notice("second@example.com")
        self.record()
        self.record(payload([team("甲队", 12), team("乙队", 9)]))
        with patch("beauclaw.mail.send_message", side_effect=[smtplib.SMTPRecipientsRefused({}), None]) as send:
            self.assertTrue(deliver_one(self.store, self.config))
            self.assertTrue(deliver_one(self.store, self.config))
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.store.summary()["mail_pending"], 1)
        self.assertEqual(self.store.summary()["mail_sent"], 1)

    def test_provider_reuse_ports_and_masked_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            configure_mail(self.config_path, "mail.provider", "aliyun")
            configure_mail(self.config_path, "mail.port", "80")
            config = load_mail_config(self.config_path)
            self.assertEqual(config["host"], "smtpdm.aliyun.com")
            self.assertEqual(config["security"], "starttls")
            self.assertNotIn("test-smtp-secret", json.dumps(show_config(self.config_path)))
            with self.assertRaises(ValueError):
                configure_mail(self.config_path, "mail.password", "test-smtp-secret")
            with self.assertRaises(ValueError):
                configure_mail(self.config_path, "mail.port", "587")
            with self.assertRaises(ValueError):
                configure_mail(self.config_path, "mail.provider", "unknown")


if __name__ == "__main__":
    unittest.main()
