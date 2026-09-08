"""One independent poll loop per ranking, with a live subscription registry."""
from __future__ import annotations

import fcntl
import os
from datetime import datetime, timedelta, timezone
import signal
import sqlite3
import threading
import time
from pathlib import Path

from beauclaw.auth import credential_metadata, credential_stamp, load_auth
from beauclaw.core import Response, Store, fetch, utcnow
from beauclaw.mail import MailWorker, load_mail_config, mail_policy
from beauclaw.rankings import Rankings, is_active
from beauclaw.providers import get_provider
from beauclaw.service import mark_worker
from beauclaw.diagnostics import event, exception_details


def report_poll(store, context, poll, events, response, failures, previous_failures, started, args):
    from beauclaw.cli import retry_delay
    duration = max(0, time.monotonic() - started)
    failed = poll["status"] == "error"
    wait = retry_delay(response, failures, args.interval) if failed else max(0, args.interval - duration)
    diagnostics = poll["diagnostics"]
    next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat(timespec="milliseconds")
    fields = {**context, "poll_id": poll["id"], "status": poll["status"], "http_status": poll["http_status"],
              "duration_ms": round(duration * 1000, 1), "consecutive_failures": failures,
              "category": diagnostics.get("category"), "next_attempt_at": next_attempt,
              "retry_in_seconds": round(wait, 3), "action": diagnostics.get("action")}
    if fields["category"] == "auth_expired" and diagnostics.get("auth", {}).get("credential_source") == "environment":
        fields["action"] = "The process uses BEAUCLAW_TOKEN; update or unset it, then restart BeauClaw"
    store.set_runtime(**fields, pid=os.getpid(), state="backoff" if failed else "waiting")
    if not failed and previous_failures:
        event("collector.recovered", "Collection recovered", **context, poll_id=poll["id"],
              previous_failures=previous_failures, duration_ms=fields["duration_ms"])
    message = poll["error"] or poll["info"].get("reason") or f"Snapshot saved; {len(events)} recorded changes"
    event("poll.failed" if failed else "poll.completed", message,
          "WARNING" if fields["category"] == "snapshot_changed" else "ERROR" if failed else "INFO",
          **fields, critical=poll["critical"], changes=len(events), counts=poll["info"].get("counts", {}),
          **({"diagnostics": diagnostics} if failed else {}))
    if not failed:
        event("poll.requests", "Request diagnostics", "DEBUG", **context, poll_id=poll["id"], diagnostics=diagnostics)
    return wait


def wait_for_retry(stop, delay, args, stamp, category, context):
    if category not in {"auth_expired", "auth_config"} or stamp is None:
        stop.wait(delay)
        return
    deadline = time.monotonic() + delay
    while not stop.is_set():
        if credential_stamp(args.auth_file, args.token_file) != stamp:
            event("auth.updated", "Credentials changed; retrying collection now", **context)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop.wait(min(1, remaining))


class Collector(threading.Thread):
    def __init__(self, ranking: dict, args):
        super().__init__(name=f"ranking-{ranking['short_id']}", daemon=True)
        self.ranking, self.args = ranking, args
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.startup_error = None
        self.finished_at = None
        self.failed = False

    def run(self):
        path = Path(self.ranking["db"])
        path.parent.mkdir(parents=True, exist_ok=True)
        store, mail, lock = None, None, None
        context = {"ranking": self.ranking["short_id"], "provider": self.ranking.get("provider", "gitcode"),
                   "competition_id": self.ranking["competition_id"]}
        try:
            lock = path.with_suffix(path.suffix + ".lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another collector already owns this ranking database") from None
            source = get_provider(self.ranking.get("provider", "gitcode"))
            store = Store(path, self.ranking["competition_id"], notice_db=self.args.db, provider=source.id)
            event("collector.started", "Ranking collector started", **context, interval_seconds=self.args.interval)
            with store.db:
                store.db.execute("INSERT OR REPLACE INTO meta VALUES ('interval_seconds',?)", (str(self.args.interval),))
            active = lambda: not self.stop_event.is_set() and is_active(self.args.db, self.ranking["short_id"], self.ranking["generation"])
            if not self.args.no_mail:
                mail = MailWorker(path, self.args.mail_config, notice_db=self.args.db, active=active)
                mail.start()
            self.ready.set()
            failures, samples = 0, 0
            last_mail_error = None
            while active():
                started = time.monotonic()
                store.set_runtime(state="fetching", pid=os.getpid(), consecutive_failures=failures,
                                  next_attempt_at=None, retry_in_seconds=None, **context)
                stamp = credential_stamp(self.args.auth_file, self.args.token_file) if source.requires_login else None
                try:
                    auth = load_auth(self.args.auth_file, self.args.token_file) if source.requires_login else {}
                    fetcher = fetch if source.id == "gitcode" else source.fetch
                    response = fetcher(self.ranking["competition_id"], auth, self.args.timeout)
                    if source.requires_login:
                        response.diagnostics["auth"] = credential_metadata(auth, self.args.token_file)
                except (OSError, ValueError) as exc:
                    message = (f"Could not read local credentials ({type(exc).__name__}); run beauclaw login"
                               if source.requires_login else f"Public leaderboard request failed ({type(exc).__name__}); retrying")
                    response = Response(utcnow(), utcnow(), "", None,
                                        error=message, diagnostics={"exception": exception_details(exc),
                                                                   "category": "auth_config" if source.requires_login else "network_connect"})
                policy = None
                if active() and not self.args.no_mail and self.args.mail_config.exists():
                    try:
                        policy = {**mail_policy(load_mail_config(self.args.mail_config), provider=source.id),
                                  "competition_name": self.ranking["name"],
                                  "ranking_id": self.ranking["short_id"],
                                  "ranking_generation": self.ranking["generation"]}
                        last_mail_error = None
                    except ValueError as exc:
                        if str(exc) != last_mail_error:
                            event("mail.config_error", "Invalid mail configuration; check beauclaw config show", "ERROR",
                                  **context, exception=exception_details(exc))
                        last_mail_error = str(exc)
                poll, events = store.record(response, self.args.missing_samples, mail_policy=policy)
                if mail:
                    mail.wake.set()
                samples += 1
                self.failed = poll["status"] == "error"
                previous_failures = failures
                failures = failures + 1 if self.failed else 0
                wait = report_poll(store, context, poll, events, response, failures, previous_failures, started, self.args)
                if self.args.once or (self.args.samples and samples >= self.args.samples):
                    break
                wait_for_retry(self.stop_event, wait, self.args, stamp, poll["diagnostics"].get("category"), context)
        except Exception as exc:
            self.startup_error = type(exc).__name__
            self.failed = True
            event("collector.crashed", "Collector stopped unexpectedly; the manager retries after 30s", "ERROR",
                  **context, exception=exception_details(exc))
        finally:
            self.ready.set()
            if mail:
                mail.close()
            if store:
                try:
                    store.set_runtime(state="crashed" if self.startup_error else "stopped", next_attempt_at=None,
                                      retry_in_seconds=None)
                except sqlite3.Error as exc:
                    event("collector.state_error", "Could not save collector shutdown state", "ERROR", **context,
                          exception=exception_details(exc))
                store.close()
            if lock:
                lock.close()
            self.finished_at = time.monotonic()
            event("collector.stopped", "Ranking collector stopped", **context)


def watch_all(args) -> int:
    from beauclaw.cli import server_for
    args.db.parent.mkdir(parents=True, exist_ok=True)
    # Individual history databases have their own locks as well.
    with args.db.with_suffix(".manager.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another BeauClaw monitor already uses this ranking registry") from None
        stop = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        workers = {}
        server = None
        limited = args.once or args.samples > 0
        with Rankings(args.db) as rankings:
            def reconcile():
                selected = {row["short_id"]: row for row in rankings.list()}
                for code, worker in list(workers.items()):
                    if code in selected and selected[code]["generation"] != worker.ranking["generation"]:
                        worker.stop_event.set()
                    if not worker.is_alive() and (worker.stop_event.is_set() or (worker.finished_at is not None and time.monotonic() - worker.finished_at >= 30)):
                        del workers[code]
                        continue
                    if code not in selected:
                        worker.stop_event.set()
                        if not worker.is_alive():
                            del workers[code]
                for code, row in selected.items():
                    if code not in workers:
                        worker = Collector(row, args)
                        workers[code] = worker
                        worker.start()
                return selected

            try:
                if not args.no_web and not args.once:
                    server = server_for(args.db, args.port)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    event("dashboard.started", f"Dashboard: http://127.0.0.1:{server.server_port}")
                selected = reconcile()
                for worker in workers.values():
                    worker.ready.wait(5)
                if workers and all(worker.startup_error for worker in workers.values()):
                    raise ValueError("No ranking collector could start; check the log for database locks")
                if limited and not selected:
                    raise ValueError("No rankings configured; use beauclaw ranking add URL")
                event("monitor.started", f"Monitoring {len(selected)} rankings, every {args.interval:g}s each", rankings=len(selected))
                mark_worker(args, "running")
                while not stop.is_set():
                    if limited:
                        if all(not worker.is_alive() for worker in workers.values()):
                            return 1 if any(worker.failed for worker in workers.values()) else 0
                    else:
                        reconcile()
                    stop.wait(0.5)
                return 0
            finally:
                for worker in workers.values():
                    worker.stop_event.set()
                for worker in workers.values():
                    worker.join(timeout=args.timeout + 15)
                if server:
                    server.shutdown()
                    server.server_close()
                mark_worker(args, "stopped")
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                event("monitor.stopped", "Monitoring stopped. Completed snapshots and mail queues have been retained.")
