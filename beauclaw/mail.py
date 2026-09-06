"""SMTP notifications with a durable outbox; delivery runs outside the poll loop."""
from __future__ import annotations

import getpass
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from beauclaw.auth import save_auth
from beauclaw.core import BOARDS, Store, utcnow

PROVIDERS = {"aliyun": {"host": "smtpdm.aliyun.com", "ports": (25, 80, 465), "port": 465}}


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", value):
        raise ValueError("请输入一个完整邮箱地址，例如 name@mail.icthub.top")
    return value


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {"provider": "aliyun", "port": 465}
    try:
        config = json.loads(path.read_text())
    except (ValueError, OSError):
        raise ValueError("无法读取配置，请运行 beauclaw config set") from None
    if not isinstance(config, dict):
        raise ValueError("邮件配置需为 JSON 对象")
    return config


def load_mail_config(path: Path) -> dict:
    config = read_settings(path)
    address(config.get("sender"))
    provider = config.get("provider", "aliyun")
    if provider not in PROVIDERS:
        raise ValueError("当前支持的服务器厂商：aliyun")
    port = config.get("port", 465)
    if type(port) is not int or port not in PROVIDERS[provider]["ports"]:
        raise ValueError("aliyun SMTP 端口支持 25、80、465")
    password = os.environ.get("BEAUCLAW_SMTP_PASSWORD") or config.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("未配置 SMTP 密码，请运行 beauclaw config set mail.password")
    return {**config, "provider": provider, "host": PROVIDERS[provider]["host"], "port": port,
            "security": "ssl" if port == 465 else "starttls", "username": config["sender"],
            "password": password, "boards": ["realtime_region_ranking"], "notify_score": True}


def mail_policy(config: dict) -> dict:
    # Only routing and notification preferences may be saved with observations.
    return {key: config[key] for key in ("sender", "boards", "notify_score")}


def configure_mail(path: Path, key: str | None = None, value: str | None = None) -> None:
    config = read_settings(path)
    if key in (None, "mail"):
        if value is not None:
            raise ValueError("交互配置不接受额外 value")
        if not sys.stdin.isatty():
            raise ValueError("请在自己的终端运行 beauclaw config set；也可逐项设置 mail.provider / mail.sender / mail.password")
        provider = input(f"服务器厂商 [{config.get('provider', 'aliyun')}]: ").strip() or config.get("provider", "aliyun")
        if provider not in PROVIDERS:
            raise ValueError("当前仅支持 aliyun")
        sender = input(f"发信地址 [{config.get('sender', '')}]: ").strip() or config.get("sender", "")
        config.update(provider=provider, sender=address(sender), port=465)
        print("复用 aliyun 服务设置：smtpdm.aliyun.com:465（SSL）；支持批量邮件发信地址。")
        print("请使用阿里云邮件推送控制台中为该发信地址设置的 SMTP 密码。")
        password = getpass.getpass("SMTP 密码（不回显；已有配置时留空保留）: ")
        if password:
            config["password"] = password
        if not config.get("password") and not os.environ.get("BEAUCLAW_SMTP_PASSWORD"):
            raise ValueError("未输入 SMTP 密码")
    else:
        key = {"mail.provider": "provider", "mail.sender": "sender", "mail.from": "sender",
               "mail.password": "password", "mail.port": "port"}.get(key)
        if key is None:
            raise ValueError("可设置 mail.provider、mail.sender、mail.password、mail.port")
        if key == "password":
            if value is not None:
                raise ValueError("密码请用 beauclaw config set mail.password 隐藏输入，不要放进命令行")
            if not sys.stdin.isatty():
                raise ValueError("请在交互终端隐藏输入密码，或使用 BEAUCLAW_SMTP_PASSWORD")
            value = getpass.getpass("阿里云邮件推送 SMTP 密码（不回显）: ")
            if not value:
                raise ValueError("未输入 SMTP 密码")
        elif value is None:
            raise ValueError("该配置项需要一个值")
        if key == "provider" and value not in PROVIDERS:
            raise ValueError("当前仅支持 aliyun")
        if key == "sender":
            value = address(value)
        if key == "port":
            try:
                value = int(value)
            except ValueError:
                raise ValueError("端口需为 25、80 或 465") from None
            if value not in (25, 80, 465):
                raise ValueError("端口需为 25、80 或 465")
        config[key] = value
    save_auth(path, config)
    print(f"发信配置已保存：{path}（权限 600）。通知收件人通过 beauclaw notice add 管理。")


def show_config(path: Path) -> dict:
    config = read_settings(path)
    provider = config.get("provider", "aliyun")
    port = config.get("port", 465)
    return {"mail.provider": provider, "mail.host": PROVIDERS.get(provider, {}).get("host"),
            "mail.port": port, "mail.security": "ssl" if port == 465 else "starttls",
            "mail.sender": config.get("sender", "未设置"),
            "mail.password": "已设置（环境变量）" if os.environ.get("BEAUCLAW_SMTP_PASSWORD") else
                             "已设置" if config.get("password") else "未设置",
            "notice.rule": "仅西北赛区榜首队伍或分数变化", "config_file": str(path)}


def create_message(payload: dict, message_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = payload["sender"]
    message["To"] = payload["recipient"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = message_id
    if payload.get("test"):
        message["Subject"] = "[BeauClaw] SMTP 测试邮件"
        message.set_content("这是一封榜单监控测试邮件。收到此邮件表示 SMTP 发信配置可用。\n")
        return message
    events = payload["events"]
    label = str(payload["schedule_name"]).replace("\r", " ").replace("\n", " ")[:80]
    message["Subject"] = f"[BeauClaw · 榜一变化] {label} · 快照 #{payload['poll_id']}"
    observed = datetime.fromisoformat(payload["captured_at"]).astimezone(timezone(timedelta(hours=8)))
    lines = ["监测到公开榜单的榜首发生变化。", f"采样时间：{observed:%Y-%m-%d %H:%M:%S}（北京时间）",
             f"赛事 ID：{payload['competition_id']}", f"原始快照编号：{payload['poll_id']}", ""]
    for event in events:
        before, after = event["before"], event["after"]
        board = event["scope"].split(":", 1)[1]
        lines.extend([f"【{BOARDS[board]}】", f"变化前：{before['name']}，分数 {before['score']}",
                      f"变化后：{after['name']}，分数 {after['score']}",
                      f"变化前快照：{event['details']['previous_poll_id']}", ""])
    lines.extend([f"榜单：https://competition.gitcode.com/competition/{payload['competition_id']}/live-ranking",
                  "", "完整原始快照保存在采集机的 SQLite 数据库，可按编号查看或导出。",
                  "这是采样时观测到的变化，不代表有人故意藏榜。"])
    message.set_content("\n".join(lines))
    return message


def send_message(config: dict, message: EmailMessage, sender: str, recipient: str) -> None:
    context = ssl.create_default_context()
    client = (smtplib.SMTP_SSL(config["host"], config["port"], timeout=10, context=context)
              if config["security"] == "ssl"
              else smtplib.SMTP(config["host"], config["port"], timeout=10))
    try:
        if config["security"] == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(config["username"], config["password"])
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # A failing QUIT must not turn an accepted message into a retry.
        client.close()


def deliver_one(store: Store, config: dict) -> bool:
    row = store.db.execute("SELECT * FROM mail_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND next_attempt <= ? ORDER BY id LIMIT 1",
                           (time.time(),)).fetchone()
    if row is None:
        return False
    payload = json.loads(row["payload_json"])
    try:
        if payload["sender"] != config["sender"]:
            raise ValueError("待发送邮件的发件人与当前配置不同")
        message = create_message(payload, row["message_id"])
        send_message(config, message, payload["sender"], payload["recipient"])
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        # Never include server text, authentication strings, or exception repr in logs.
        error = f"{type(exc).__name__}" + (f" (SMTP {exc.smtp_code})" if hasattr(exc, "smtp_code") else "")
        attempts = row["attempts"] + 1
        delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
        with store.db:
            store.db.execute("UPDATE mail_outbox SET attempts=?,next_attempt=?,last_error=? WHERE id=?",
                             (attempts, time.time() + delay, error, row["id"]))
        print(f"邮件 #{row['id']} 发送失败：{error}；{delay}s 后重试。", flush=True)
    else:
        with store.db:
            store.db.execute("UPDATE mail_outbox SET attempts=attempts+1,sent_at=?,last_error=NULL WHERE id=?",
                             (utcnow(), row["id"]))
        print(f"榜一变化通知 #{row['id']} 已被 SMTP 服务器接受（快照 #{row['poll_id']}）。", flush=True)
    return True


class MailWorker(threading.Thread):
    def __init__(self, db_path: Path, config_path: Path):
        super().__init__(name="smtp-notifications", daemon=True)
        self.db_path, self.config_path = db_path, config_path
        self.stop_event, self.wake = threading.Event(), threading.Event()

    def run(self):
        store = Store(self.db_path)
        try:
            while not self.stop_event.is_set():
                try:
                    if self.config_path.exists():
                        config = load_mail_config(self.config_path)
                        if deliver_one(store, config):
                            continue
                except ValueError:
                    pass  # The poll loop reports invalid configuration; keep the queue intact.
                self.wake.wait(2)
                self.wake.clear()
        finally:
            store.close()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.join(timeout=12)


def test_mail(path: Path, store: Store) -> None:
    config = load_mail_config(path)
    notices = store.notices()
    if not notices:
        raise ValueError("通知列表为空，请先运行 beauclaw notice add 邮箱地址")
    failed = 0
    for notice in notices:
        payload = {"test": True, "sender": config["sender"], "recipient": notice["email"]}
        try:
            send_message(config, create_message(payload, make_msgid(domain="beauclaw.local")),
                         config["sender"], notice["email"])
        except (OSError, smtplib.SMTPException) as exc:
            failed += 1
            print(f"{notice['email']}：发送失败（{type(exc).__name__}）")
        else:
            print(f"{notice['email']}：测试邮件已被 SMTP 接受")
    if failed:
        raise ValueError(f"{failed} 个收件人的测试邮件未被 SMTP 接受")
