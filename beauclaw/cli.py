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
            store = Store(self.db_path)
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
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with args.db.with_suffix(args.db.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有进程正在采集到此数据库，请不要重复启动") from None
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
                print(f"查看记录：http://127.0.0.1:{server.server_port}", flush=True)
            print(f"赛事 {args.competition}，采样间隔 {args.interval:g}s，数据库 {args.db}", flush=True)
            if not args.no_mail:
                mail_worker = MailWorker(args.db, args.mail_config)
                mail_worker.start()
                print("邮件通知：已配置" if args.mail_config.exists() else "邮件通知：未配置，请运行 beauclaw config set", flush=True)
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
                                        error=f"读取本地凭据失败（{type(exc).__name__}），请检查文件或重新登录")
                policy = None
                if not args.no_mail and args.mail_config.exists():
                    try:
                        policy = mail_policy(load_mail_config(args.mail_config))
                        last_mail_error = None
                    except ValueError as exc:
                        if str(exc) != last_mail_error:
                            print(f"邮件通知配置错误：{exc}", flush=True)
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
                      + (f"；{delay:g}s 后重试" if failed else ""), flush=True)
                for event in events:
                    if event["kind"] == "rank_changed":
                        continue  # All rank changes are persisted; keep the terminal readable.
                    before, after = event["before"], event["after"]
                    change = f" {before['score']} → {after['score']}" if before and after else ""
                    print(f"  {KINDS[event['kind']]} {event['name']}{change} [{event['scope']}]", flush=True)
                if args.once or (args.samples and samples >= args.samples):
                    break
                # Fixed start-to-start interval; no overlapping requests or catch-up bursts.
                wait = delay if failed else max(0, args.interval - (time.monotonic() - started))
                stop.wait(wait)
            print("采集已停止，所有已完成快照均已保存。", flush=True)
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
    print(f"已导出变化记录：{output}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="beauclaw", description="BeauClaw：每 10 秒监控 CANN 榜单，西北赛区榜一变化时邮件通知")
    parser.add_argument("--version", action="version", version=f"beauclaw {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="配置发信邮箱与服务器厂商")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    for action in ("set", "show"):
        command = config_commands.add_parser(action)
        command.add_argument("--config-file", type=Path, default=config_dir() / "mail.json")
        if action == "set":
            command.add_argument("key", nargs="?", help="省略时交互配置；支持 mail.provider / mail.sender / mail.password / mail.port")
            command.add_argument("value", nargs="?", help="密码不能作为参数传入")
    notice = commands.add_parser("notice", help="管理通知收件邮箱")
    notice_commands = notice.add_subparsers(dest="notice_command", required=True)
    for action in ("add", "list", "delete", "test"):
        command = notice_commands.add_parser(action)
        command.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
        if action in ("add", "delete"):
            command.add_argument("addresses", nargs="+", help="邮箱地址；delete 也接受 list 中的编号")
        if action == "test":
            command.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
    test = commands.add_parser("test", help="向通知列表中所有邮箱发送测试邮件",
                               description="使用当前 SMTP 配置，向通知列表中所有邮箱各发送一封测试邮件。")
    test.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
    test.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
    test.set_defaults(notice_command="test")
    for name in ("login", "watch", "start", "stop", "status", "serve", "export", "snapshot"):
        command = commands.add_parser(name)
        if name in ("login", "watch", "start"):
            command.add_argument("--competition", type=competition_id, default=DEFAULT_COMPETITION, help="赛事 ID 或榜单链接")
            command.add_argument("--auth-file", type=Path, default=config_dir() / "gitcode.json")
            command.add_argument("--token-file", type=Path, help="只含网页 Token 的本地文件")
        if name != "login":
            command.add_argument("--db", type=Path, default=data_dir() / "beauclaw.sqlite3")
        if name in ("watch", "start"):
            command.add_argument("--mail-config", type=Path, default=config_dir() / "mail.json")
        if name == "login":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--browser", action="store_true", help="打开浏览器手动登录，然后自动保存会话")
            modes.add_argument("--from-gc", action="store_true", help="尝试复用 gc 凭据；赛事接口可能不接受 PAT")
            command.add_argument("--profile", type=Path, default=data_dir() / "browser-profile")
        if name in ("watch", "start", "serve"):
            command.add_argument("--port", type=int, default=8765)
        if name in ("watch", "start"):
            command.add_argument("--interval", type=float, default=10)
            command.add_argument("--timeout", type=float, default=8)
            command.add_argument("--missing-samples", type=int, default=2, help="缺席多少份有效非空快照后标为持续未出现")
            if name == "watch":
                command.add_argument("--once", action="store_true", help="抓一次，用于验证登录和接口")
                command.add_argument("--samples", type=int, default=0, help="抓取指定次数后退出，0 为持续运行")
                command.add_argument("--service-token", help=argparse.SUPPRESS)
                command.add_argument("--service-file", type=Path, help=argparse.SUPPRESS)
            command.add_argument("--no-web", action="store_true")
            command.add_argument("--no-mail", action="store_true", help="暂停入队和发送邮件")
        if name == "stop":
            command.add_argument("--timeout", type=float, default=30)
            command.add_argument("--force", action="store_true", help="超时后终止 BeauClaw 专用 tmux 会话")
        if name == "status":
            command.add_argument("--json", action="store_true", help="输出进程和采样状态 JSON")
        if name in ("export", "snapshot"):
            command.add_argument("--output", type=Path, required=True)
        if name == "snapshot":
            command.add_argument("id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "login":
            if args.token_file and (args.browser or args.from_gc):
                raise ValueError("--token-file 不能与 --browser / --from-gc 同时使用")
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
                        print(f"{'已添加' if added else '已存在'}：{email.strip().lower()}")
                elif args.notice_command == "list":
                    rows = store.notices()
                    if not rows:
                        print("通知列表为空。使用 beauclaw notice add 邮箱地址 添加。")
                    for row in rows:
                        print(f"{row['id']}\t{row['email']}\t{row['created_at']}")
                elif args.notice_command == "delete":
                    for key in args.addresses:
                        print(f"已删除：{store.delete_notice(key)}，该邮箱排队中的未发送通知已取消。")
                elif args.notice_command == "test":
                    test_mail(args.mail_config, store)
            finally:
                store.close()
        elif args.command in ("watch", "start"):
            if not all(math.isfinite(n) and n >= 1 for n in (args.interval, args.timeout)) or args.missing_samples < 1 or getattr(args, "samples", 0) < 0:
                raise ValueError("间隔和超时需至少 1 秒，缺席样本数需至少 1，采集次数不能为负数")
            if not 1 <= args.port <= 65535:
                raise ValueError("网页端口需在 1 到 65535 之间")
            if args.command == "watch":
                return watch(args)
            state = Service(args.db).start(args)
            print(f"{'已经在运行' if state['already_running'] else '已通过 tmux 启动'}：PID {state['pid']}")
            print(f"日志：{state['log']}")
            if state["dashboard"]:
                print(f"查看记录：{state['dashboard']}")
            print("使用 beauclaw status 查看采样状态，beauclaw stop 停止。")
        elif args.command == "stop":
            if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 60:
                raise ValueError("停止等待时间需在 1 到 60 秒之间")
            stopped = Service(args.db).stop(args.timeout, args.force)
            print("BeauClaw 已停止，历史数据已保留。" if stopped else "BeauClaw 当前未运行。")
        elif args.command == "status":
            state = Service(args.db).status()
            summary = None
            if args.db.is_file():
                store = Store(args.db)
                try:
                    summary = store.summary()
                    summary.pop("states")
                    summary.pop("kinds")
                finally:
                    store.close()
            if args.json:
                print(json.dumps({"service": state, "observations": summary}, ensure_ascii=False, indent=2))
            else:
                print(f"运行状态：{ {'running': '运行中', 'starting': '启动中', 'stopped': '已停止'}[state['state']]} ({state['state']})")
                if state["pid"]:
                    print(f"PID：{state['pid']}；启动时间：{state['started_at']}；版本：{state['version']}")
                    print(f"进入 tmux：{state['attach_command']}")
                print(f"日志：{state['log']}\n数据库：{state['db']}")
                if summary and summary["latest"]:
                    latest = summary["latest"]
                    print(f"最近采样：{latest['captured_at']} {latest['status']} {latest['error'] or latest['info'].get('reason', '')}")
                    print(f"快照：{summary['poll_count']}；通知邮箱：{summary['notice_count']}；邮件待发：{summary['mail_pending']}；SMTP 已接受：{summary['mail_sent']}")
                else:
                    print("尚无采样记录。")
        else:
            if not args.db.is_file():
                raise ValueError("尚无数据库，请先运行 watch")
            if args.command == "serve":
                with server_for(args.db, args.port) as server:
                    print(f"历史记录：http://127.0.0.1:{server.server_port}（此命令不采集新数据）", flush=True)
                    server.serve_forever()
            else:
                store = Store(args.db)
                try:
                    if args.command == "export":
                        export_events(store, args.output)
                    elif args.command == "snapshot":
                        data = store.snapshot(args.id)
                        if data is None:
                            raise ValueError("找不到指定快照")
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"已导出原始快照：{args.output}")
                finally:
                    store.close()
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
