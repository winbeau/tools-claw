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
from email.headerregistry import Address
from email.utils import format_datetime, make_msgid
from pathlib import Path

from beauclaw.auth import load_auth, save_auth
from beauclaw.core import Store, fetch, parse_board, should_notify_leader, top_ten, utcnow
from beauclaw.email_template import render_email
from beauclaw.ui import activity
from beauclaw.paths import config_dir

PROVIDERS = {"aliyun": {"host": "smtpdm.aliyun.com", "ports": (25, 80, 465), "port": 465}}


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", value):
        raise ValueError("Enter a complete email address, for example name@mail.icthub.top")
    return value


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {"provider": "aliyun", "port": 465}
    try:
        config = json.loads(path.read_text())
    except (ValueError, OSError):
        raise ValueError("Could not read mail configuration; run beauclaw config set") from None
    if not isinstance(config, dict):
        raise ValueError("Mail configuration must be a JSON object")
    return config


def load_mail_config(path: Path) -> dict:
    config = read_settings(path)
    address(config.get("sender"))
    provider = config.get("provider", "aliyun")
    if provider not in PROVIDERS:
        raise ValueError("Supported mail provider: aliyun")
    port = config.get("port", 465)
    if type(port) is not int or port not in PROVIDERS[provider]["ports"]:
        raise ValueError("Aliyun SMTP ports: 25, 80, 465")
    password = os.environ.get("BEAUCLAW_SMTP_PASSWORD") or config.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("SMTP password is missing; run beauclaw config set mail.password")
    return {**config, "provider": provider, "host": PROVIDERS[provider]["host"], "port": port,
            "security": "ssl" if port == 465 else "starttls", "username": config["sender"],
            "password": password, "boards": ["realtime_region_ranking"]}


def mail_policy(config: dict) -> dict:
    # Only routing and notification preferences may be saved with observations.
    return {key: config[key] for key in ("sender", "boards")}


def configure_mail(path: Path, key: str | None = None, value: str | None = None) -> None:
    config = read_settings(path)
    if key in (None, "mail"):
        if value is not None:
            raise ValueError("Interactive configuration does not accept a value argument")
        if not sys.stdin.isatty():
            raise ValueError("Run beauclaw config set in an interactive terminal, or set mail.provider / mail.sender / mail.password individually")
        provider = input(f"Mail provider [{config.get('provider', 'aliyun')}]: ").strip() or config.get("provider", "aliyun")
        if provider not in PROVIDERS:
            raise ValueError("Only aliyun is currently supported")
        sender = input(f"Sender address [{config.get('sender', '')}]: ").strip() or config.get("sender", "")
        config.update(provider=provider, sender=address(sender), port=465)
        print("Using aliyun: smtpdm.aliyun.com:465 (SSL), including batch-mail senders.")
        print("Use the SMTP password configured for this sender in the Aliyun DirectMail console.")
        password = getpass.getpass("SMTP password (hidden; leave empty to keep the current password): ")
        if password:
            config["password"] = password
        if not config.get("password") and not os.environ.get("BEAUCLAW_SMTP_PASSWORD"):
            raise ValueError("No SMTP password was entered")
    else:
        key = {"mail.provider": "provider", "mail.sender": "sender", "mail.from": "sender",
               "mail.password": "password", "mail.port": "port"}.get(key)
        if key is None:
            raise ValueError("Supported keys: mail.provider, mail.sender, mail.password, mail.port")
        if key == "password":
            if value is not None:
                raise ValueError("Use beauclaw config set mail.password for hidden input; do not pass passwords as command arguments")
            if not sys.stdin.isatty():
                raise ValueError("Enter the password in an interactive terminal, or set BEAUCLAW_SMTP_PASSWORD")
            value = getpass.getpass("Aliyun DirectMail SMTP password (hidden): ")
            if not value:
                raise ValueError("No SMTP password was entered")
        elif value is None:
            raise ValueError("This setting requires a value")
        if key == "provider" and value not in PROVIDERS:
            raise ValueError("Only aliyun is currently supported")
        if key == "sender":
            value = address(value)
        if key == "port":
            try:
                value = int(value)
            except ValueError:
                raise ValueError("Port must be 25, 80, or 465") from None
            if value not in (25, 80, 465):
                raise ValueError("Port must be 25, 80, or 465")
        config[key] = value
    save_auth(path, config)
    print(f"Mail configuration saved: {path} (mode 600). Manage recipients with beauclaw notice add.")


def show_config(path: Path) -> dict:
    config = read_settings(path)
    provider = config.get("provider", "aliyun")
    port = config.get("port", 465)
    return {"mail.provider": provider, "mail.host": PROVIDERS.get(provider, {}).get("host"),
            "mail.port": port, "mail.security": "ssl" if port == 465 else "starttls",
            "mail.sender": config.get("sender", "not set"),
            "mail.password": "set (environment)" if os.environ.get("BEAUCLAW_SMTP_PASSWORD") else
                             "set" if config.get("password") else "not set",
            "notice.rule": "Only a regional leader's score rise or a change of leading team; score drops remain critical records", "config_file": str(path)}


def create_message(payload: dict, message_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = Address(display_name="ICTHub", addr_spec=payload["sender"])
    message["To"] = payload["recipient"]
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = message_id
    if not payload.get("test"):
        message["Importance"] = "high"
        message["X-Priority"] = "1"
    subject, plain, html = render_email(payload)
    message["Subject"] = subject
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
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
    # Apply the current rule to old queued jobs as well as newly captured changes.
    payload["events"] = [event for event in payload["events"] if should_notify_leader(event)]
    if not payload["events"]:
        with store.db:
            store.db.execute("UPDATE mail_outbox SET cancelled_at=?,last_error=NULL WHERE id=?", (utcnow(), row["id"]))
        return True
    if payload.get("ranking_id"):
        from beauclaw.rankings import is_active
        if not is_active(store.notice_db, payload["ranking_id"], payload.get("ranking_generation")):
            with store.db:
                store.db.execute("UPDATE mail_outbox SET cancelled_at=? WHERE id=?", (utcnow(), row["id"]))
            return True
    current_notice = next((notice for notice in store.notices() if notice["email"] == payload["recipient"]), None)
    if current_notice is None or (payload.get("recipient_id") and current_notice["id"] != payload["recipient_id"]) or (payload.get("recipient_created_at") and
                                 current_notice["created_at"] != payload["recipient_created_at"]):
        with store.db:
            store.db.execute("UPDATE mail_outbox SET cancelled_at=? WHERE id=?", (utcnow(), row["id"]))
        return True
    if "top10" not in payload:
        payload["top10"] = store.ranking_snapshot(row["poll_id"]).get("top10", [])
    try:
        if payload["sender"] != config["sender"]:
            raise ValueError("Queued sender does not match the current mail configuration")
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
        print(f"Email #{row['id']} failed: {error}; retry in {delay}s.", flush=True)
    else:
        with store.db:
            store.db.execute("UPDATE mail_outbox SET attempts=attempts+1,sent_at=?,last_error=NULL WHERE id=?",
                             (utcnow(), row["id"]))
        print(f"Leader-change email #{row['id']} accepted by SMTP (snapshot #{row['poll_id']}).", flush=True)
    return True


class MailWorker(threading.Thread):
    def __init__(self, db_path: Path, config_path: Path, notice_db: Path | None = None, active=None):
        super().__init__(name="smtp-notifications", daemon=True)
        self.db_path, self.config_path = db_path, config_path
        self.notice_db, self.active = notice_db, active
        self.stop_event, self.wake = threading.Event(), threading.Event()

    def run(self):
        store = Store(self.db_path, notice_db=self.notice_db)
        try:
            while not self.stop_event.is_set():
                try:
                    if self.config_path.exists() and (self.active is None or self.active()):
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


def test_mail(path: Path, store: Store, auth_file: Path | None = None, token_file: Path | None = None,
              recipient: str | None = None) -> None:
    from beauclaw.rankings import Rankings
    if recipient is not None:
        recipient = address(recipient.strip()).lower()
    with Rankings(store.path) as registry:
        rankings = registry.list()
    if not rankings:
        raise ValueError("Rankings is empty; no leaderboard is being monitored.")
    config = load_mail_config(path)
    notices = [{"email": recipient}] if recipient is not None else store.notices()
    if not notices:
        raise ValueError("The recipient list is empty; run beauclaw notice add EMAIL first")
    first = rankings[0]
    with activity(f"Fetching the first ranking: {first['short_id']}-{first['name']}..."):
        credentials = load_auth(auth_file or config_dir() / "gitcode.json", token_file)
        response = fetch(first["competition_id"], credentials)
        if response.error or response.status != 200:
            raise ValueError(response.error or f"Leaderboard request returned HTTP {response.status}; run beauclaw login")
        board = parse_board(response.body, response.captured_at)
        rows = top_ten(board)
        if board["status"] != "ok" or not rows:
            raise ValueError("The regional leaderboard is empty or unavailable; no test email was sent")
    snapshot = {"competition_id": first["competition_id"], "competition_name": first["name"],
                "captured_at": response.captured_at, "schedule_name": board["schedule_name"], "top10": rows}
    failed = 0
    for index, notice in enumerate(notices, 1):
        payload = {**snapshot, "test": True, "sender": config["sender"], "recipient": notice["email"]}
        try:
            with activity(f"Sending test email {index}/{len(notices)} to {notice['email']}..."):
                send_message(config, create_message(payload, make_msgid(domain="beauclaw.local")),
                             config["sender"], notice["email"])
        except (OSError, smtplib.SMTPException) as exc:
            failed += 1
            print(f"{notice['email']}: delivery failed ({type(exc).__name__})")
        else:
            print(f"{notice['email']}: test email accepted by SMTP")
    if failed:
        raise ValueError(f"Test email was not accepted by SMTP for {failed} recipient(s)")
