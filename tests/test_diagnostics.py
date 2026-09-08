import base64
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from beauclaw import cli, tianchi
from beauclaw.auth import credential_metadata, credential_stamp, save_auth
from beauclaw.core import DEFAULT_COMPETITION, Response, Store, fetch_url
from beauclaw.diagnostics import (LOGGER, PrivateRotatingHandler, event, exception_details, log_files, log_session,
                                  read_logs, remember_secrets, sanitize)
from beauclaw.monitor import Collector, report_poll
from beauclaw.rankings import Rankings
from test_beauclaw import TIME, response
from test_tianchi import captured, fixture


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "beauclaw.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_rotating_logs_retain_errors_and_filter_before_tail(self):
        original = list(LOGGER.handlers)
        with log_session(self.db, console=False, max_bytes=1300, backups=2):
            event("poll.failed", "session expired", "ERROR", ranking="abcdef", poll_id=1)
            for i in range(30):
                event("poll.completed", f"sample {i}", ranking="abcdef" if i % 2 else "123456", poll_id=i + 2)
        self.assertEqual(LOGGER.handlers, original)
        paths = [p for p in log_files(self.db.with_suffix(".log")) if p.exists()]
        self.assertEqual(len(paths), 3)
        for path in [*paths, self.db.with_suffix(".errors.log")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertLessEqual(path.stat().st_size, 1300)
        selected = list(read_logs(self.db.with_suffix(".log"), ranking="abcdef", lines=2))
        self.assertEqual([r["poll_id"] for r in selected], [29, 31])
        errors = list(read_logs(self.db.with_suffix(".errors.log"), level="ERROR"))
        self.assertEqual([r["event"] for r in errors], ["poll.failed"])

    def test_follow_survives_rotation_and_truncation_without_duplicates(self):
        path = self.db.with_suffix(".log")
        path.write_text("")
        seen, ready, stop = [], threading.Event(), threading.Event()

        def consume():
            ready.set()
            for item in read_logs(path, lines=0, follow=True, stop=stop):
                seen.append(item["message"])
        worker = threading.Thread(target=consume)
        worker.start()
        try:
            self.assertTrue(ready.wait(1))
            # Let the reader open the current file before appending.
            time.sleep(0.05)
            for i in range(3):
                text = json.dumps({"level": "INFO", "message": str(i)}) + "\n"
                if i == 1:
                    path.rename(path.with_suffix(".log.1"))
                    path.write_text("")
                if i == 2:
                    path.write_text("")
                    time.sleep(0.3)  # Allow copy-truncate detection before the new write.
                with path.open("a") as stream:
                    stream.write(text)
                deadline = time.monotonic() + 2
                while len(seen) < i + 1 and time.monotonic() < deadline:
                    time.sleep(0.02)
            self.assertEqual(seen, ["0", "1", "2"])
        finally:
            stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_redaction_and_exception_locations_exclude_secrets(self):
        secret = "synthetic-opaque-private-value"
        remember_secrets({"password": secret})
        remember_secrets({"token": "1"})
        self.assertEqual(sanitize({"time": TIME, "password": "1"}), {"time": TIME, "password": "[redacted]"})
        with log_session(self.db, "DEBUG", console=False):
            try:
                raise ValueError(f"server echoed {secret}")
            except ValueError as exc:
                event("test", f"server echoed {secret}; Bearer private-other; user@example.com", "ERROR",
                      exception=exception_details(exc), headers={"Authorization": "private-other", "Set-Cookie": secret},
                      endpoint="https://alice:private@api.example.test/rank?raceId=123&token=hidden",
                      raw_body="never log response bodies", password=secret)
        text = self.db.with_suffix(".log").read_text()
        for forbidden in (secret, "private-other", "user@example.com", "alice:", "token=hidden", "never log response bodies"):
            self.assertNotIn(forbidden, text)
        value = json.loads(text)
        self.assertEqual(value["exception"][0]["type"], "ValueError")
        self.assertTrue(value["exception"][0]["frames"])
        self.assertEqual(value["endpoint"], "https://api.example.test/rank?raceId=123")
        self.assertNotIn("locals", text)

    def test_follow_handles_partial_records_and_a_crashed_rotated_file(self):
        path = self.db.with_suffix(".log")
        path.write_text('{"level":"INFO","message":"采集')
        seen, stop = [], threading.Event()
        def consume():
            for item in read_logs(path, lines=10, follow=True, stop=stop):
                seen.append(item["message"])
        worker = threading.Thread(target=consume)
        worker.start()
        try:
            time.sleep(0.25)
            self.assertEqual(seen, [])
            with path.open("a") as stream:
                stream.write('成功"}\n')
            deadline = time.monotonic() + 2
            while not seen and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(seen, ["采集成功"])
            with path.open("a") as stream:
                stream.write('{"message":"crash')
            time.sleep(0.25)
            path.rename(path.with_suffix(".log.1"))
            path.write_text('{"message":"restarted","level":"INFO"}\n')
            deadline = time.monotonic() + 2
            while len(seen) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(seen, ["采集成功", "restarted"])
        finally:
            stop.set()
            worker.join(2)

    def test_http_auth_failure_keeps_safe_api_diagnostics(self):
        body = b'{"error_code":420,"error_code_name":"SESSION_EXPIRED","trace_id":"trace-123","error_message":"do not log this"}'
        error = HTTPError("https://api.example.test/rank", 401, "Unauthorized",
                          {"X-Request-ID": "request-123", "Set-Cookie": "secret-session"}, io.BytesIO(body))
        with patch("beauclaw.core.urlopen", side_effect=error):
            sample = fetch_url(error.url, {"Authorization": "Bearer hidden"})
        with contextlib.closing(Store(self.db, DEFAULT_COMPETITION)) as store:
            poll, events = store.record(sample)
            self.assertEqual(events, [])
            diagnostic = poll["diagnostics"]
            self.assertEqual(diagnostic["category"], "auth_expired")
            self.assertEqual(diagnostic["api"]["error_code"], 420)
            self.assertEqual(diagnostic["headers"]["x-request-id"], "request-123")
            self.assertIn("--browser", diagnostic["action"])
            self.assertNotIn("do not log this", json.dumps(diagnostic))
            self.assertNotIn("secret-session", json.dumps(diagnostic))
            self.assertEqual(store.snapshot(poll["id"])["diagnostics"], diagnostic)

    def test_network_failure_distinguishes_timeout_dns_and_tls(self):
        for cause, category in ((TimeoutError("hidden"), "network_timeout"),
                                (socket.gaierror(-2, "hidden"), "network_dns"),
                                (ssl.SSLError("hidden"), "network_tls"),
                                (ConnectionRefusedError(111, "hidden"), "network_connect")):
            with self.subTest(category=category), patch("beauclaw.core.urlopen", side_effect=URLError(cause)):
                sample = fetch_url("https://api.example.test/rank", {})
                self.assertEqual(sample.diagnostics["category"], category)
                self.assertIn(category, sample.error)
                self.assertIn(type(cause).__name__, json.dumps(sample.diagnostics))
                self.assertNotIn("hidden", json.dumps(sample.diagnostics))

    def test_parser_failure_keeps_field_cause_and_does_not_advance_state(self):
        with contextlib.closing(Store(self.db, "532499", provider="tianchi")) as store:
            store.record(captured())
            state = store.summary()["states"]
            broken = fixture()
            detail = json.loads(broken["detail"]["body"])
            del detail["data"]["race"]
            broken["detail"]["body"] = json.dumps(detail)
            poll, events = store.record(captured(broken))
            self.assertEqual(poll["diagnostics"]["category"], "schema_error")
            self.assertIn("missing field: race", poll["error"])
            cause = next(e for e in poll["diagnostics"]["exception"] if e["type"] == "KeyError")
            self.assertEqual(cause["missing_field"], "race")
            self.assertEqual(poll["diagnostics"]["validation"], {"phase": "detail"})
            self.assertEqual(store.summary()["states"], state)
            self.assertEqual(events, [])
            self.assertEqual(store.summary()["mail_pending"], 0)

    def test_invalid_tianchi_score_identifies_page_and_row(self):
        broken = fixture()
        page = json.loads(broken["pages"][1]["body"])
        page["data"]["list"][3]["score"] = None
        broken["pages"][1]["body"] = json.dumps(page)
        with contextlib.closing(Store(self.db, "532499", provider="tianchi")) as store:
            poll, _ = store.record(captured(broken))
            self.assertEqual(poll["status"], "error")
            self.assertEqual(poll["diagnostics"]["validation"], {"phase": "page", "page": 2, "row": 4})

    def test_log_write_failure_reports_once_without_stopping_collection(self):
        output = io.StringIO()
        with log_session(self.db, console=False), contextlib.redirect_stderr(output), \
             patch.object(PrivateRotatingHandler, "shouldRollover", side_effect=OSError("secret disk exception")):
            for _ in range(2):
                event("test", "sensitive record", "ERROR")
        self.assertEqual(output.getvalue().count("could not write a log file"), 2)  # One per destination.
        self.assertNotIn("secret disk exception", output.getvalue())
        self.assertNotIn("sensitive record", output.getvalue())

    def test_tianchi_failed_page_retains_phase_status_and_retry_after(self):
        bundle = fixture()

        def request(url, headers, timeout):
            if "getDetail" in url:
                return Response(TIME, TIME, url, 200, bundle["detail"]["body"].encode())
            number = int(parse_qs(urlsplit(url).query)["pageNum"][0])
            if number == 2:
                return Response(TIME, TIME, url, 429, b'{}', {"retry-after": "120"})
            return Response(TIME, TIME, url, 200, bundle["pages"][number - 1]["body"].encode())
        with patch("beauclaw.tianchi.fetch_url", side_effect=request):
            sample = tianchi.fetch("532499")
        with contextlib.closing(Store(self.db, "532499", provider="tianchi")) as store:
            poll, _ = store.record(sample)
            failed = next(r for r in poll["diagnostics"]["requests"] if r["http_status"] == 429)
            self.assertEqual((failed["phase"], failed["page"]), ("page", 2))
            self.assertEqual(poll["diagnostics"]["category"], "rate_limit")
            self.assertEqual(cli.retry_delay(sample, 1, 10), 120)

    def test_runtime_and_log_share_backoff_and_recovery_details(self):
        args = SimpleNamespace(interval=10)
        context = {"ranking": "abcdef", "provider": "gitcode"}
        with contextlib.closing(Store(self.db, DEFAULT_COMPETITION)) as store, log_session(self.db, console=False):
            sample = response(http_status=401)
            poll, events = store.record(sample)
            delay = report_poll(store, context, poll, events, sample, 3, 2, time.monotonic(), args)
            runtime = store.summary()["runtime"]
            self.assertEqual(delay, 60)
            self.assertEqual((runtime["state"], runtime["category"], runtime["consecutive_failures"]), ("backoff", "auth_expired", 3))
            self.assertTrue(runtime["next_attempt_at"].endswith("+00:00"))
            sample = response()
            poll, events = store.record(sample)
            report_poll(store, context, poll, events, sample, 0, 3, time.monotonic(), args)
            self.assertEqual(store.summary()["runtime"]["consecutive_failures"], 0)
        rows = list(read_logs(self.db.with_suffix(".log")))
        recovered = next(r for r in rows if r["event"] == "collector.recovered")
        self.assertEqual(recovered["previous_failures"], 3)
        failure = next(r for r in rows if r["event"] == "poll.failed")
        self.assertEqual(failure["next_attempt_at"], runtime["next_attempt_at"])

    def test_credentials_change_interrupts_auth_backoff_and_recovers(self):
        with Rankings(self.db) as registry:
            row = registry.add(DEFAULT_COMPETITION, name="Fixture")
        auth = self.root / "auth.json"
        save_auth(auth, {"token": "old-synthetic-token"})
        args = SimpleNamespace(db=self.db, interval=10, timeout=1, missing_samples=2, no_mail=True,
                               auth_file=auth, token_file=None, once=False, samples=2)
        calls = []

        def request(event_id, credentials, timeout):
            calls.append(credentials["token"])
            if len(calls) == 1:
                save_auth(auth, {"token": "new-synthetic-token"})
                return response(http_status=401)
            return response()
        with patch.dict(os.environ, {"BEAUCLAW_TOKEN": ""}), patch("beauclaw.monitor.fetch", side_effect=request), log_session(self.db, console=False):
            collector = Collector(row, args)
            collector.start()
            collector.join(3)
            try:
                self.assertFalse(collector.is_alive(), "Credential update should not wait for the 60s auth backoff")
                self.assertFalse(collector.failed)
                self.assertEqual(calls, ["old-synthetic-token", "new-synthetic-token"])
            finally:
                collector.stop_event.set()
                collector.join(2)
        logs = list(read_logs(self.db.with_suffix(".log")))
        self.assertIn("auth.updated", [r["event"] for r in logs])
        self.assertIn("collector.recovered", [r["event"] for r in logs])

    def test_expiry_hint_and_environment_precedence_expose_no_claims(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1, "private_claim": "hidden"}).encode()).decode().rstrip("=")
        token = "header." + payload + ".signature"
        with patch.dict(os.environ, {"BEAUCLAW_TOKEN": token}):
            metadata = credential_metadata({"token": token})
            self.assertEqual(metadata["credential_source"], "environment")
            self.assertTrue(metadata["expired"])
            self.assertIsNone(credential_stamp(self.root / "auth"))
            self.assertNotIn("hidden", json.dumps(metadata))
            self.assertNotIn(token, json.dumps(metadata))
            self.assertEqual(credential_stamp(self.root / "auth", self.root / "explicit"), ())

    def test_unexpected_collector_crash_is_logged_without_exception_values(self):
        with Rankings(self.db) as registry:
            row = registry.add(DEFAULT_COMPETITION, name="Fixture")
        args = SimpleNamespace(db=self.db, interval=10, timeout=1, missing_samples=2, no_mail=True,
                               auth_file=self.root / "auth", token_file=None, once=True, samples=0)
        with patch("beauclaw.monitor.fetch", side_effect=RuntimeError("secret server text")), log_session(self.db, console=False):
            worker = Collector(row, args)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(worker.failed)
        log = self.db.with_suffix(".errors.log").read_text()
        self.assertIn("collector.crashed", log)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("secret server text", log)
        with contextlib.closing(Store(Path(row["db"]))) as history:
            self.assertEqual(history.summary()["runtime"]["state"], "crashed")

    def test_old_database_migrates_without_altering_snapshots(self):
        with contextlib.closing(Store(self.db, DEFAULT_COMPETITION)) as store:
            poll, _ = store.record(response())
            before = store.snapshot(poll["id"])
            with store.db:
                store.db.execute("ALTER TABLE polls DROP COLUMN diagnostics_json")
        with contextlib.closing(Store(self.db)) as store:
            after = store.snapshot(poll["id"])
            self.assertEqual(after["diagnostics"], {})
            for key in ("id", "raw_body", "body_sha256", "captured_at", "critical"):
                self.assertEqual(after[key], before[key])

    def test_logs_cli_reads_archives_and_validates_filters(self):
        with log_session(self.db, console=False):
            event("failed", "fixture error", "ERROR", ranking="abcdef")
            event("ok", "fixture success", ranking="123456")
        output = io.StringIO()
        with patch("sys.argv", ["beauclaw", "logs", "--db", str(self.db), "--ranking", "ABCDEF", "--errors", "--json"]), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["event"], "failed")
        with patch("sys.argv", ["beauclaw", "logs", "--lines", "-1"]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 1)

    def test_concurrent_workers_write_intact_json_records(self):
        with log_session(self.db, console=False):
            workers = [threading.Thread(target=lambda n=n: [event("test", "fixture", ranking=f"{n:06x}", sequence=i) for i in range(50)]) for n in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(3)
        rows = list(read_logs(self.db.with_suffix(".log"), lines=1000))
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({(r["ranking"], r["sequence"]) for r in rows}), 200)


if __name__ == "__main__":
    unittest.main()
