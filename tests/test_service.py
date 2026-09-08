import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from beauclaw.service import Service
from beauclaw.core import Store, DEFAULT_COMPETITION
from beauclaw.rankings import Rankings


@unittest.skipUnless(shutil.which("tmux"), "requires tmux")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="beauclaw tmux '")
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "BEAUCLAW_DATA_DIR": str(self.root / "data"),
                    "BEAUCLAW_CONFIG_DIR": str(self.root / "config")}
        self.db = self.root / "data" / "beauclaw.sqlite3"
        history = Store(self.db, DEFAULT_COMPETITION)
        history.close()
        with Rankings(self.db):
            pass
        self.service = Service(self.db)
        self.token = self.root / "empty-token"
        self.token.write_text("")  # Local validation fails; no remote HTTP or email is sent.

    def tearDown(self):
        self.call("stop", "--timeout", "2", "--force")
        self.service.tmux("kill-server")  # Only the dedicated socket of this test.
        self.tmp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, "-m", "beauclaw", *args], env=self.env,
                              capture_output=True, text=True, timeout=20, cwd="/tmp")

    def start(self):
        return self.call("start", "--no-web", "--no-mail", "--token-file", str(self.token))

    def test_start_idempotence_status_and_stop_preserve_data(self):
        initial = self.call("status", "--json")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertFalse(json.loads(initial.stdout)["service"]["running"])
        started = self.start()
        self.assertEqual(started.returncode, 0, started.stderr)
        status = json.loads(self.call("status", "--json").stdout)
        pid = status["service"]["pid"]
        self.assertTrue(status["service"]["running"])
        self.assertGreater(pid, 1)
        again = self.start()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Already running", again.stdout)
        self.assertEqual(json.loads(self.call("status", "--json").stdout)["service"]["pid"], pid)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = json.loads(self.call("status", "--json").stdout)
            if status["observations"] and status["observations"]["poll_count"]:
                break
            time.sleep(0.1)
        self.assertGreater(status["observations"]["poll_count"], 0)
        self.assertEqual(status["observations"]["latest"]["status"], "error")
        self.assertEqual(status["rankings"][0]["observations"]["runtime"]["state"], "backoff")
        logs = self.call("logs", "--errors", "--json")
        self.assertEqual(logs.returncode, 0, logs.stderr)
        failure = next(row for row in map(json.loads, logs.stdout.splitlines()) if row["event"] == "poll.failed")
        self.assertEqual(failure["pid"], pid)
        self.assertEqual(failure["category"], "auth_config")
        self.assertTrue(failure["next_attempt_at"])
        self.assertEqual(Path(status["service"]["error_log"]).stat().st_mode & 0o777, 0o600)
        stopped = self.call("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        status = json.loads(self.call("status", "--json").stdout)
        self.assertFalse(status["service"]["running"])
        self.assertGreater(status["observations"]["poll_count"], 0)
        self.assertTrue(self.service.log_path.is_file())
        self.assertEqual(self.call("stop").returncode, 0)

    def test_stop_does_not_kill_other_sessions(self):
        result = self.service.tmux("new-session", "-d", "-s", "other", "/bin/sh", "-c", "sleep 60")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.call("stop").returncode, 0)
        self.assertEqual(self.service.tmux("has-session", "-t", "other").returncode, 0)

    def test_existing_foreground_collector_is_not_reported_as_new_background_worker(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.db.with_suffix(self.db.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = self.start()
        self.assertEqual(started.returncode, 1)
        self.assertIn("did not start", started.stderr)
        self.assertFalse(json.loads(self.call("status", "--json").stdout)["service"]["running"])

    def test_running_monitor_picks_up_added_and_deleted_rankings(self):
        self.assertEqual(self.start().returncode, 0)
        initial = json.loads(self.call("status", "--json").stdout)
        original = initial["rankings"][0]
        added = self.call("ranking", "add", "https://competition.gitcode.com/competition/12345/live-ranking", "--name", "Second fixture")
        self.assertEqual(added.returncode, 0, added.stderr)
        deadline = time.monotonic() + 5
        second = None
        while time.monotonic() < deadline:
            state = json.loads(self.call("status", "--json").stdout)
            second = next((r for r in state["rankings"] if r["competition_id"] == "12345"), None)
            if second and second["observations"] and second["observations"]["poll_count"]:
                break
            time.sleep(0.1)
        self.assertIsNotNone(second)
        self.assertGreater(second["observations"]["poll_count"], 0)
        self.assertEqual(state["service"]["pid"], initial["service"]["pid"])
        self.assertEqual(self.call("ranking", "delete", original["short_id"]).returncode, 0)
        state = json.loads(self.call("status", "--json").stdout)
        self.assertEqual([r["short_id"] for r in state["rankings"]], [second["short_id"]])
        self.assertTrue(state["service"]["running"])
        self.assertTrue(Path(original["db"]).is_file())
        # Wait until the removed collector releases its history database lock.
        deadline = time.monotonic() + 5
        with self.db.with_suffix(self.db.suffix + ".lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
        self.assertEqual(self.call("ranking", "add", original["url"]).returncode, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.call("status", "--json").stdout)
            row = next(r for r in state["rankings"] if r["short_id"] == original["short_id"])
            if row["observations"] and row["observations"]["poll_count"] >= 2:
                break
            time.sleep(0.1)
        self.assertGreaterEqual(row["observations"]["poll_count"], 2)

    def test_stale_state_does_not_signal_an_unrelated_pid(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.service.state_path.write_text(json.dumps({"pid": os.getpid(), "state": "running"}))
        result = self.call("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not running", result.stdout)


if __name__ == "__main__":
    unittest.main()
