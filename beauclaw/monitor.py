"""One independent poll loop per ranking, with a live subscription registry."""
from __future__ import annotations

import fcntl
import signal
import sqlite3
import threading
import time
from pathlib import Path

from beauclaw.auth import load_auth
from beauclaw.core import Response, Store, fetch, utcnow
from beauclaw.mail import MailWorker, load_mail_config, mail_policy
from beauclaw.rankings import Rankings, is_active
from beauclaw.providers import get_provider
from beauclaw.service import mark_worker


class Collector(threading.Thread):
    def __init__(self, ranking: dict, args):
        super().__init__(name=f"ranking-{ranking['short_id']}", daemon=True)
        self.ranking, self.args = ranking, args
        self.stop_event, self.ready = threading.Event(), threading.Event()
        self.startup_error = None
        self.finished_at = None
        self.failed = False

    def run(self):
        from beauclaw.cli import retry_delay
        path = Path(self.ranking["db"])
        path.parent.mkdir(parents=True, exist_ok=True)
        store, mail, lock = None, None, None
        try:
            lock = path.with_suffix(path.suffix + ".lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another collector already owns this ranking database") from None
            source = get_provider(self.ranking.get("provider", "gitcode"))
            store = Store(path, self.ranking["competition_id"], notice_db=self.args.db, provider=source.id)
            with store.db:
                store.db.execute("INSERT OR REPLACE INTO meta VALUES ('interval_seconds',?)", (str(self.args.interval),))
            active = lambda: not self.stop_event.is_set() and is_active(self.args.db, self.ranking["short_id"], self.ranking["generation"])
            if not self.args.no_mail:
                mail = MailWorker(path, self.args.mail_config, notice_db=self.args.db, active=active)
                mail.start()
            self.ready.set()
            failures, samples = 0, 0
            while active():
                started = time.monotonic()
                try:
                    auth = load_auth(self.args.auth_file, self.args.token_file) if source.requires_login else {}
                    fetcher = fetch if source.id == "gitcode" else source.fetch
                    response = fetcher(self.ranking["competition_id"], auth, self.args.timeout)
                except (OSError, ValueError) as exc:
                    message = (f"Could not read local credentials ({type(exc).__name__}); run beauclaw login"
                               if source.requires_login else f"Public leaderboard request failed ({type(exc).__name__}); retrying")
                    response = Response(utcnow(), utcnow(), "", None,
                                        error=message)
                policy = None
                if active() and not self.args.no_mail and self.args.mail_config.exists():
                    try:
                        policy = {**mail_policy(load_mail_config(self.args.mail_config), provider=source.id),
                                  "competition_name": self.ranking["name"],
                                  "ranking_id": self.ranking["short_id"],
                                  "ranking_generation": self.ranking["generation"]}
                    except ValueError as exc:
                        print(f"[{self.ranking['short_id']}] Mail configuration: {exc}", flush=True)
                poll, events = store.record(response, self.args.missing_samples, mail_policy=policy)
                if mail:
                    mail.wake.set()
                samples += 1
                self.failed = poll["status"] == "error"
                failures = failures + 1 if self.failed else 0
                detail = poll["error"] or poll["info"].get("reason") or f"{len(events)} recorded changes"
                print(f"[{poll['captured_at']}] [{self.ranking['short_id']}] #{poll['id']} {poll['status']} - {detail}", flush=True)
                if self.args.once or (self.args.samples and samples >= self.args.samples):
                    break
                wait = retry_delay(response, failures, self.args.interval) if self.failed else max(0, self.args.interval - (time.monotonic() - started))
                self.stop_event.wait(wait)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.startup_error = str(exc)
            self.failed = True
            print(f"[{self.ranking['short_id']}] Collector stopped: {exc}", flush=True)
        finally:
            self.ready.set()
            if mail:
                mail.close()
            if store:
                store.close()
            if lock:
                lock.close()
            self.finished_at = time.monotonic()


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
                    print(f"Dashboard: http://127.0.0.1:{server.server_port}", flush=True)
                selected = reconcile()
                for worker in workers.values():
                    worker.ready.wait(5)
                if workers and all(worker.startup_error for worker in workers.values()):
                    raise ValueError("No ranking collector could start; check the log for database locks")
                if limited and not selected:
                    raise ValueError("No rankings configured; use beauclaw ranking add URL")
                print(f"Monitoring {len(selected)} rankings, every {args.interval:g}s each. Ranking and recipient changes apply automatically.", flush=True)
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
                print("Monitoring stopped. Completed snapshots and mail queues have been retained.", flush=True)
