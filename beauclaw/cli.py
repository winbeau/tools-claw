#!/usr/bin/env python3
"""A 10-second CANN leaderboard recorder with a local dashboard."""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from beauclaw.auth import load_auth, login
from beauclaw.core import DEFAULT_COMPETITION, KINDS, Response, Store, competition_id, dumps, fetch, utcnow
from beauclaw.mail import MailWorker, configure_mail, load_mail_config, mail_policy, show_config, test_mail
from beauclaw.paths import config_dir, data_dir
from beauclaw import __version__
from beauclaw.service import Service, mark_worker
from beauclaw.rankings import Rankings, ranking_status
from beauclaw.ui import activity, set_animation

ROOT = Path(__file__).resolve().parent


def retry_delay(response: Response, failures: int, interval: float) -> float:
    if failures == 0:
        return interval
    delay = max(interval, min(300, 10 * 2 ** min(failures - 1, 5)))
    if response.status in (400, 401, 403, 418):
        delay = max(delay, 60)
    retry = response.headers.get("retry-after")
    if retry:
        try:
            requested = float(retry)
        except ValueError:
            try:
                requested = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                requested = 0
        if math.isfinite(requested):
            delay = max(delay, requested)
    return delay


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, db_path: Path, **kwargs):
        self.db_path = db_path
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        # The service is local-only; do not allow DNS rebinding through an arbitrary Host.
        if self.headers.get("Host", "").split(":")[0] not in ("localhost", "127.0.0.1"):
            self.send_error(403)
            return
        path = urlsplit(self.path)
        try:
            if path.path == "/":
                self.reply((ROOT / "assets" / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
                return
            query = parse_qs(path.query)
            with Rankings(self.db_path) as rankings:
                rows = rankings.list()
                if path.path == "/api/rankings":
                    self.reply(dumps([{key: row[key] for key in ("short_id", "competition_id", "provider", "name", "url")} for row in rows]).encode(), "application/json; charset=utf-8")
                    return
                selected = rankings.get(query["ranking"][0]) if query.get("ranking") else rows[0] if rows else None
            store = Store(Path(selected["db"]) if selected else self.db_path, notice_db=self.db_path,
                          provider=selected["provider"] if selected else None)
            try:
                if path.path == "/api/summary":
                    result = store.summary()
                elif path.path == "/api/events":
                    query = parse_qs(path.query)
                    before = int(query["before"][0]) if "before" in query else None
                    result = store.events(limit=200, before_id=before, signals_only=query.get("signals") == ["1"])
                elif path.path.startswith("/api/snapshots/"):
                    result = store.snapshot(int(path.path.rsplit("/", 1)[-1]))
                    if result is None:
                        self.send_error(404)
                        return
                else:
                    self.send_error(404)
                    return
            finally:
                store.close()
            self.reply(dumps(result).encode(), "application/json; charset=utf-8")
        except (ValueError, OverflowError):
            self.send_error(400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)


def server_for(db_path: Path, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), partial(DashboardHandler, db_path=db_path))


def watch(args) -> int:
    if args.competition is None:
        from beauclaw.monitor import watch_all
        return watch_all(args)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with args.db.with_suffix(args.db.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another collector is already writing to this database") from None
        store = Store(args.db, args.competition)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO meta VALUES ('interval_seconds',?)", (str(args.interval),))
        stop = threading.Event()
        previous_signals = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        server = None
        mail_worker = None
        try:
            if not args.no_web and not args.once:
                server = server_for(args.db, args.port)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                print(f"Dashboard: http://127.0.0.1:{server.server_port}", flush=True)
            print(f"Competition {args.competition}; interval {args.interval:g}s; database {args.db}", flush=True)
            if not args.no_mail:
                mail_worker = MailWorker(args.db, args.mail_config)
                mail_worker.start()
                print("Mail notifications: configured" if args.mail_config.exists() else "Mail notifications: not configured; run beauclaw config set", flush=True)
            failures, samples, failed = 0, 0, False
            last_mail_error = None
            mark_worker(args, "running")
            while not stop.is_set():
                started = time.monotonic()
                try:
                    auth = load_auth(args.auth_file, args.token_file)
                    response = fetch(args.competition, auth, args.timeout)
                except (OSError, ValueError) as exc:
                    response = Response(utcnow(), utcnow(), "", None,
                                        error=f"Could not read local credentials ({type(exc).__name__}); check the file or sign in again")
                policy = None
                if not args.no_mail and args.mail_config.exists():
                    try:
                        policy = mail_policy(load_mail_config(args.mail_config))
                        last_mail_error = None
                    except ValueError as exc:
                        if str(exc) != last_mail_error:
                            print(f"Mail configuration error: {exc}", flush=True)
                        last_mail_error = str(exc)
                poll, events = store.record(response, args.missing_samples, mail_policy=policy)
                if mail_worker:
                    mail_worker.wake.set()
                samples += 1
                failed = poll["status"] == "error"
                failures = failures + 1 if failed else 0
                message = poll["error"] or poll["info"].get("reason") or " / ".join(
                    f"{key.removeprefix('realtime_')}: {count}" for key, count in poll["info"]["counts"].items())
                delay = retry_delay(response, failures, args.interval)
                print(f"[{poll['captured_at']}] #{poll['id']} {poll['status']} {message}"
                      + (f"; retry in {delay:g}s" if failed else ""), flush=True)
                for event in events:
                    if event["kind"] == "rank_changed":
                        continue  # All rank changes are persisted; keep the terminal readable.
                    before, after = event["before"], event["after"]
                    change = f" {before['score']} → {after['score']}" if before and after else ""
                    print(f"  {event['kind'].replace('_', ' ')} {event['name']}{change} [{event['scope']}]", flush=True)
                if args.once or (args.samples and samples >= args.samples):
                    break
                # Fixed start-to-start interval; no overlapping requests or catch-up bursts.
                wait = delay if failed else max(0, args.interval - (time.monotonic() - started))
                stop.wait(wait)
            print("Collection stopped. All completed snapshots have been saved.", flush=True)
            return 1 if (args.once or args.samples) and failed else 0
        finally:
            if mail_worker:
                mail_worker.close()
            if server:
                server.shutdown()
                server.server_close()
            store.close()
            mark_worker(args, "stopped")
            for sig, handler in previous_signals.items():
                signal.signal(sig, handler)


def export_events(store: Store, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["事件ID", "快照ID", "采样时间UTC", "赛程/榜单", "变化", "队伍", "原分数", "新分数", "原排名", "新排名", "详情"])
        for event in store.db.execute("SELECT * FROM events ORDER BY id"):
            before, after = json.loads(event["before_json"]) or {}, json.loads(event["after_json"]) or {}
            values = [event["id"], event["poll_id"], event["observed_at"], event["scope"], KINDS[event["kind"]],
                      event["name"], before.get("score", ""), after.get("score", ""), before.get("rank", ""),
                      after.get("rank", ""), event["details_json"]]
            # Protect spreadsheet users from formula-like team names or other text cells.
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values])
    print(f"Exported events: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="beauclaw", description="BeauClaw: monitor GitCode and Aliyun Tianchi leaderboards every 10 seconds; email ICTHub alerts when the leader's score rises or the leading team changes")
    parser.add_argument("--version", action="version", version=f"beauclaw {__version__}")
    parser.add_argument("--no-animation", action="store_true", help="Disable terminal animations")
    commands = parser.add_subparsers(dest="command", required=True)
    ranking = commands.add_parser("ranking", help="Manage monitored competition leaderboards")
    ranking_commands = ranking.add_subparsers(dest="ranking_command", required=True)
    for action in ("add", "list", "delete"):
        command = ranking_commands.add_parser(action)
        command.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
        if action == "add":
            command.add_argument("url", help="GitCode live-ranking URL or Aliyun Tianchi rankingList URL")
            command.add_argument("--name", help="Optional display name")
        elif action == "delete":
            command.add_argument("id", help="Six-character ranking ID from ranking list")
    config = commands.add_parser("config", help="Configure the sender and mail provider")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    for action in ("set", "show"):
        command = config_commands.add_parser(action)
        command.add_argument("--config-file", type=Path, default=config_dir() / "mail.json")
        if action == "set":
            command.add_argument("key", nargs="?", help="Omit for interactive setup; keys: mail.provider / mail.sender / mail.password / mail.port")
            command.add_argument("value", nargs="?", help="Passwords cannot be passed as command arguments")
    notice = commands.add_parser("notice", help="Manage notification recipients")
    notice_commands = notice.add_subparsers(dest="notice_command", required=True)
    for action in ("add", "list", "delete", "test"):
        command = notice_commands.add_parser(action)
        command.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
        if action in ("add", "delete"):
            command.add_argument("addresses", nargs="+", help="Email addresses; delete accepts a six-character ID from notice list")
        if action == "test":
            command.add_argument("recipient", nargs="?", help="Send only to this email address without adding it to the recipient list; omit to notify everyone")
            command.add_argument("--ranking", help="Use this six-character ranking ID; defaults to the first ranking")
            command.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
            command.add_argument("--auth-file", type=Path, default=config_dir() / "gitcode.json")
            command.add_argument("--token-file", type=Path)
    test = commands.add_parser("test", help="Send a test email to every notification recipient",
                               description="Use the current SMTP configuration to send one test email to every notification recipient.")
    test.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
    test.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
    test.add_argument("--auth-file", type=Path, default=config_dir() / "gitcode.json")
    test.add_argument("--token-file", type=Path)
    test.add_argument("--ranking", help="Use this six-character ranking ID; defaults to the first ranking")
    test.set_defaults(notice_command="test")
    for name in ("login", "watch", "start", "stop", "status", "serve", "export", "snapshot"):
        command = commands.add_parser(name)
        if name in ("login", "watch", "start"):
            command.add_argument("--competition", type=competition_id, default=DEFAULT_COMPETITION if name == "login" else None, help="Legacy single GitCode competition ID or URL; otherwise monitor all providers in ranking list")
            command.add_argument("--auth-file", type=Path, default=config_dir() / "gitcode.json")
            command.add_argument("--token-file", type=Path, help="Local file containing only the web token")
        if name != "login":
            command.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
        if name in ("watch", "start"):
            command.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
        if name == "login":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--browser", action="store_true", help="Open a browser for sign-in and save the verified session")
            modes.add_argument("--from-gc", action="store_true", help="Try gc credentials; the competition API may not accept personal access tokens")
            command.add_argument("--profile", type=Path, default=data_dir() / "browser-profile")
        if name in ("watch", "start", "serve"):
            command.add_argument("--port", type=int, default=8765)
        if name in ("watch", "start"):
            command.add_argument("--interval", type=float, default=10)
            command.add_argument("--timeout", type=float, default=8)
            command.add_argument("--missing-samples", type=int, default=2, help="Number of valid nonempty snapshots before confirming a missing team")
            if name == "watch":
                command.add_argument("--once", action="store_true", help="Fetch once to check credentials and API access")
                command.add_argument("--samples", type=int, default=0, help="Exit after this many samples per ranking; 0 runs continuously")
                command.add_argument("--service-token", help=argparse.SUPPRESS)
                command.add_argument("--service-file", type=Path, help=argparse.SUPPRESS)
            command.add_argument("--no-web", action="store_true")
            command.add_argument("--no-mail", action="store_true", help="Disable mail queuing and delivery")
        if name == "stop":
            command.add_argument("--timeout", type=float, default=30)
            command.add_argument("--force", action="store_true", help="Terminate the dedicated BeauClaw tmux session after the timeout")
        if name == "status":
            command.add_argument("--json", action="store_true", help="Output process and sampling status as JSON")
        if name in ("export", "snapshot"):
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--ranking", help="Six-character ranking ID to export")
        if name == "snapshot":
            command.add_argument("id", type=int)
    args = parser.parse_args()
    set_animation(not args.no_animation)
    try:
        if args.command == "ranking":
            with Rankings(args.db) as registry:
                if args.ranking_command == "add":
                    with activity("Loading competition details..."):
                        row = registry.add(args.url, args.name)
                    print(f"{row['short_id']}-{row['name']} -> {row['url']}")
                    print("Ranking enabled. A running monitor will pick it up automatically.")
                elif args.ranking_command == "delete":
                    row = registry.delete(args.id)
                    print(f"Removed {row['short_id']}-{row['name']}. History retained; pending mail cancelled.")
                else:
                    rows = registry.list()
                    for row in rows:
                        print(f"{row['short_id']}-{row['name']} -> {row['url']}")
                    if not rows:
                        print("No rankings configured. Use beauclaw ranking add URL.")
        elif args.command == "login":
            if args.token_file and (args.browser or args.from_gc):
                raise ValueError("--token-file cannot be combined with --browser / --from-gc")
            login(args.competition, args.auth_file, browser=args.browser, from_gc=args.from_gc,
                  token_file=args.token_file, profile=args.profile)
        elif args.command == "config":
            if args.config_command == "set":
                configure_mail(args.config_file, args.key, args.value)
            else:
                print(json.dumps(show_config(args.config_file), ensure_ascii=False, indent=2))
        elif args.command in ("notice", "test"):
            store = Store(args.db)
            try:
                if args.notice_command == "add":
                    for email in args.addresses:
                        added = store.add_notice(email)
                        row = next(row for row in store.notices() if row['email'] == email.strip().lower())
                        print(f"{'Added' if added else 'Already present'}: {row['short_id']}-{row['email']}")
                elif args.notice_command == "list":
                    rows = store.notices()
                    if not rows:
                        print("No recipients configured. Use beauclaw notice add EMAIL.")
                    for row in rows:
                        print(f"{row['short_id']}-{row['email']}")
                elif args.notice_command == "delete":
                    for key in args.addresses:
                        email = store.delete_notice(key)
                        with Rankings(args.db) as registry:
                            registry.cancel_recipient(email)
                        print(f"Removed: {email}. Pending notifications cancelled across all rankings.")
                elif args.notice_command == "test":
                    test_mail(args.mail_config, store, args.auth_file, args.token_file,
                              recipient=getattr(args, "recipient", None), ranking_id=args.ranking)
            finally:
                store.close()
        elif args.command in ("watch", "start"):
            if not all(math.isfinite(n) and n >= 1 for n in (args.interval, args.timeout)) or args.missing_samples < 1 or getattr(args, "samples", 0) < 0:
                raise ValueError("Interval and timeout must be at least 1 second; missing samples at least 1; sample count nonnegative")
            if not 1 <= args.port <= 65535:
                raise ValueError("Dashboard port must be between 1 and 65535")
            if args.command == "watch":
                return watch(args)
            with activity("Starting the tmux monitor..."):
                state = Service(args.db).start(args)
            print(f"{'Already running' if state['already_running'] else 'Started in tmux'}: PID {state['pid']}")
            print(f"Log: {state['log']}")
            if state["dashboard"]:
                print(f"Dashboard: {state['dashboard']}")
            print("Use beauclaw status to inspect sampling, or beauclaw stop to stop.")
        elif args.command == "stop":
            if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 60:
                raise ValueError("Stop timeout must be between 1 and 60 seconds")
            with activity("Stopping the monitor and saving pending work..."):
                stopped = Service(args.db).stop(args.timeout, args.force)
            print("BeauClaw stopped. History retained." if stopped else "BeauClaw is not running.")
        elif args.command == "status":
            state = Service(args.db).status()
            monitored = ranking_status(args.db)
            summary = None
            if args.db.is_file():
                store = Store(args.db)
                try:
                    summary = store.summary()
                    summary.pop("states")
                    summary.pop("kinds")
                finally:
                    store.close()
            samples = [row["observations"] for row in monitored if row["observations"]]
            if samples:
                summary = {**samples[0]}
                for key in ("poll_count", "critical_poll_count", "event_count", "mail_pending", "mail_sent"):
                    summary[key] = sum(item[key] for item in samples)
                latest = [item["latest"] for item in samples if item["latest"]]
                summary["latest"] = max(latest, key=lambda item: item["captured_at"]) if latest else None
            if args.json:
                print(json.dumps({"service": state, "observations": summary, "rankings": monitored}, ensure_ascii=False, indent=2))
            else:
                print(f"Service: { {'running': 'Running', 'starting': 'Starting', 'stopped': 'Stopped'}[state['state']]} ({state['state']})")
                if state["pid"]:
                    print(f"PID: {state['pid']}; started: {state['started_at']}; version: {state['version']}")
                    print(f"Attach: {state['attach_command']}")
                print(f"Log: {state['log']}\nDatabase: {state['db']}")
                if summary and summary["latest"]:
                    latest = summary["latest"]
                    print(f"Latest sample: {latest['captured_at']} {latest['status']} {latest['error'] or latest['info'].get('reason', '')}")
                    print(f"Snapshots: {summary['poll_count']}; recipients: {summary['notice_count']}; pending mail: {summary['mail_pending']}; SMTP accepted: {summary['mail_sent']}")
                    print(f"Critical snapshots: {summary['critical_poll_count']} (retained); ordinary snapshot limit: {summary['snapshot_limit']} per ranking; active comparison baselines protected")
                else:
                    print("No samples recorded yet.")
                print(f"Monitored rankings: {len(monitored)}")
                for row in monitored:
                    latest = (row["observations"] or {}).get("latest")
                    detail = f"{latest['status']} at {latest['captured_at']}" if latest else "waiting for the first sample"
                    print(f"  {row['short_id']}-{row['name']}: {detail}")
        else:
            if not args.db.is_file():
                raise ValueError("No database found; start monitoring first")
            if args.command == "serve":
                with server_for(args.db, args.port) as server:
                    print(f"History: http://127.0.0.1:{server.server_port} (history only; no new samples)", flush=True)
                    server.serve_forever()
            else:
                with Rankings(args.db) as registry:
                    rows = registry.list()
                    selected = registry.get(args.ranking) if args.ranking else rows[0] if len(rows) == 1 else None
                if selected:
                    args.db = Path(selected["db"])
                elif len(rows) > 1:
                    raise ValueError("Choose a ranking to export with --ranking ID")
                if not args.db.is_file():
                    raise ValueError("This ranking has no recorded snapshots yet")
                store = Store(args.db)
                try:
                    if args.command == "export":
                        export_events(store, args.output)
                    elif args.command == "snapshot":
                        data = store.snapshot(args.id)
                        if data is None:
                            raise ValueError("Snapshot not found")
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"Exported raw snapshot: {args.output}")
                finally:
                    store.close()
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
