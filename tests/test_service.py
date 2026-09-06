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


@unittest.skipUnless(shutil.which("tmux"), "requires tmux")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="beauclaw tmux '")
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "BEAUCLAW_DATA_DIR": str(self.root / "data"),
                    "BEAUCLAW_CONFIG_DIR": str(self.root / "config")}
        self.db = self.root / "data" / "beauclaw.sqlite3"
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
        self.assertIn("已经在运行", again.stdout)
        self.assertEqual(json.loads(self.call("status", "--json").stdout)["service"]["pid"], pid)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = json.loads(self.call("status", "--json").stdout)
            if status["observations"] and status["observations"]["poll_count"]:
                break
            time.sleep(0.1)
        self.assertGreater(status["observations"]["poll_count"], 0)
        self.assertEqual(status["observations"]["latest"]["status"], "error")
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
        self.db.parent.mkdir(parents=True)
        with self.db.with_suffix(self.db.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = self.start()
        self.assertEqual(started.returncode, 1)
        self.assertIn("未成功启动", started.stderr)
        self.assertFalse(json.loads(self.call("status", "--json").stdout)["service"]["running"])

    def test_stale_state_does_not_signal_an_unrelated_pid(self):
        self.db.parent.mkdir(parents=True)
        self.service.state_path.write_text(json.dumps({"pid": os.getpid(), "state": "running"}))
        result = self.call("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("当前未运行", result.stdout)


if __name__ == "__main__":
    unittest.main()
