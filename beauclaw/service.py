"""Manage one detached tmux worker per database without touching other sessions."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from beauclaw import __version__
from beauclaw.auth import save_auth
from beauclaw.core import utcnow


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_worker(args, state: str) -> None:
    token = getattr(args, "service_token", None)
    path = getattr(args, "service_file", None)
    if not token or not path:
        return
    saved = read_state(path)
    if saved.get("token") != token:
        raise ValueError("Worker identity changed; refusing to overwrite another worker state")
    saved.update(state=state, pid=os.getpid(), version=__version__)
    saved["started_at" if state == "running" else "stopped_at"] = utcnow()
    save_auth(path, saved)


class Service:
    def __init__(self, db: Path):
        self.db = db.expanduser().resolve()
        digest = hashlib.sha256(str(self.db).encode()).hexdigest()[:12]
        self.socket = f"beauclaw-{digest}"
        self.session = "beauclaw"
        self.state_path = self.db.with_suffix(".service.json")
        self.log_path = self.db.with_suffix(".log")
        self.lock_path = self.db.with_suffix(".control.lock")

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        if not shutil.which("tmux"):
            raise ValueError("tmux is missing; reinstall BeauClaw or install tmux")
        env = {key: value for key, value in os.environ.items() if key != "TMUX"}
        try:
            return subprocess.run(["tmux", "-L", self.socket, "-f", "/dev/null", *args],
                                  env=env, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            raise ValueError("tmux command timed out; check the local tmux server") from None

    def pane_pid(self) -> int | None:
        if not shutil.which("tmux"):
            return None
        result = self.tmux("display-message", "-p", "-t", f"{self.session}:0.0", "#{pane_pid}")
        try:
            return int(result.stdout.strip()) if result.returncode == 0 else None
        except ValueError:
            return None

    def status(self) -> dict:
        saved = read_state(self.state_path)
        pid = self.pane_pid()
        running = pid is not None and saved.get("pid") == pid and saved.get("state") == "running"
        return {"state": "running" if running else "starting" if pid else "stopped",
                "running": running, "pid": pid, "started_at": saved.get("started_at"),
                "version": saved.get("version"), "db": str(self.db), "log": str(self.log_path),
                "dashboard": saved.get("dashboard"), "socket": self.socket, "session": self.session,
                "attach_command": shlex.join(["tmux", "-L", self.socket, "attach", "-t", self.session])}

    def start(self, args) -> dict:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.status()
            if current["state"] == "running":
                return {**current, "already_running": True}
            if current["state"] == "starting":
                raise ValueError(f"tmux session is not ready; check the log: {self.log_path}")
            token = uuid.uuid4().hex
            options = ["--db", str(self.db),
                       "--interval", str(args.interval), "--timeout", str(args.timeout),
                       "--missing-samples", str(args.missing_samples), "--port", str(args.port),
                       "--auth-file", str(args.auth_file.expanduser().resolve()),
                       "--mail-config", str(args.mail_config.expanduser().resolve()),
                       "--service-token", token, "--service-file", str(self.state_path)]
            if args.competition:
                options.extend(["--competition", args.competition])
            if args.token_file:
                options.extend(["--token-file", str(args.token_file.expanduser().resolve())])
            for flag in ("no_web", "no_mail"):
                if getattr(args, flag):
                    options.append("--" + flag.replace("_", "-"))
            save_auth(self.state_path, {"token": token, "state": "starting", "version": __version__,
                      "dashboard": None if args.no_web else f"http://127.0.0.1:{args.port}"})
            command = [sys.executable, "-u", "-m", "beauclaw", "watch", *options]
            shell = f"exec {shlex.join(command)} >> {shlex.quote(str(self.log_path))} 2>&1"
            result = self.tmux("new-session", "-d", "-s", self.session, "-c", str(self.db.parent),
                               "/bin/sh", "-c", shell)
            if result.returncode:
                raise ValueError(f"tmux could not start: {result.stderr.strip()}")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                current = self.status()
                if current["running"]:
                    return {**current, "already_running": False}
                if current["state"] == "stopped":
                    break
                time.sleep(0.1)
            raise ValueError(f"Background worker did not start; check the log: {self.log_path}")

    def stop(self, timeout: float = 30, force: bool = False) -> bool:
        if not self.db.parent.exists():
            return False
        with self.lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            status = self.status()
            if status["state"] == "stopped":
                return False
            if not status["running"]:
                raise ValueError("Session is not ready to stop; retry shortly or inspect the log")
            # Match the live tmux pane, never signal a PID from an old state file alone.
            pid = status["pid"]
            if self.pane_pid() != pid:
                return False
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return False
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.pane_pid() is None:
                    return True
                time.sleep(0.1)
            if force:
                result = self.tmux("kill-session", "-t", self.session)
                if result.returncode == 0 or self.pane_pid() is None:
                    return True
            raise ValueError("Graceful stop timed out; inspect the log or run beauclaw stop --force")
