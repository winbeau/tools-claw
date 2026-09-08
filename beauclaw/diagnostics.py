"""Private, rotating structured logs and safe collection diagnostics."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from beauclaw import __version__

LOGGER = logging.getLogger("beauclaw")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5
_secrets: set[str] = set()
_secret_lock = threading.Lock()
_sensitive = re.compile(r"token|password|passwd|secret|cookie|authorization|credential$|raw_body|^body$", re.I)


def remember_secrets(values: dict) -> None:
    """Defense in depth: never write credentials even if an error echoes them."""
    with _secret_lock:
        for key, value in values.items():
            # Short invalid credentials must not redact digits out of timestamps
            # or status categories. Sensitive fields are always removed separately.
            if _sensitive.search(key) and isinstance(value, str) and len(value) >= 8:
                _secrets.add(value)


def safe_text(value: str) -> str:
    with _secret_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, "[redacted]")
    value = re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "[redacted]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)\b(token|password|passwd|secret|cookie|authorization)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email]", value)
    return value[:4000]


def safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        # Only public leaderboard routing parameters belong in diagnostics.
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                           if k in {"raceId", "pageNum", "season", "__s"}])
        return safe_text(urlunsplit((parts.scheme, host, parts.path, query, "")))
    except ValueError:
        return "[invalid URL]"


def sanitize(value, key: str = ""):
    if _sensitive.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return safe_url(value) if key in {"url", "endpoint"} else safe_text(value)
    return value if value is None or isinstance(value, (bool, int, float)) else type(value).__name__


def exception_details(exc: BaseException) -> list[dict]:
    """Traceback locations and cause types, without values, source text or locals."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 8:
        seen.add(id(exc))
        frames, trace = [], exc.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({"file": Path(code.co_filename).name, "line": trace.tb_lineno, "function": code.co_name})
            trace = trace.tb_next
        item = {"type": type(exc).__name__, "frames": frames[-12:]}
        if isinstance(exc, KeyError) and exc.args and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(exc.args[0])):
            item["missing_field"] = str(exc.args[0])
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            item["errno"] = exc.errno
        chain.append(item)
        reason = getattr(exc, "reason", None)
        exc = exc.__cause__ or exc.__context__ or (reason if isinstance(reason, BaseException) else None)
    return chain


def network_category(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "network_timeout"
    if isinstance(reason, socket.gaierror):
        return "network_dns"
    if isinstance(reason, ssl.SSLError):
        return "network_tls"
    return "network_connect"


def api_metadata(body: bytes) -> dict:
    """Keep only bounded machine codes and request IDs from an API envelope."""
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("code", "error_code", "error_code_name", "trace_id", "requestId"):
        candidate = value.get(key)
        if type(candidate) is int or isinstance(candidate, str) and re.fullmatch(r"[\w.:-]{1,128}", candidate):
            result[key] = candidate
    return result


def failure_category(status: int | None, error: str | None, diagnostics: dict) -> str | None:
    if not error:
        return None
    if status == 401:
        return "auth_expired"
    if status == 429:
        return "rate_limit"
    if status in (403, 418):
        return "access_denied"
    if status is not None and status != 200:
        return "http_error"
    if diagnostics.get("category"):
        return diagnostics["category"]
    message = error.lower()
    if "credentials" in message:
        return "auth_config"
    if "timeout" in message:
        return "network_timeout"
    if "changed during pagination" in message or "pagination changed" in message or "ranking order changed" in message:
        return "snapshot_changed"
    if "business error" in message or "successful public leaderboard response" in message:
        return "api_error"
    return "schema_error"


ACTIONS = {
    "auth_expired": "Run beauclaw login --browser; saved credential changes trigger an immediate retry",
    "auth_config": "Check the credentials file or run beauclaw login --browser",
    "rate_limit": "Waiting for the server's Retry-After or exponential backoff",
    "access_denied": "Check site access and login; collection will retry with backoff",
    "http_error": "The upstream API returned an HTTP error; collection will retry with backoff",
    "network_timeout": "The request deadline was exceeded; check connectivity or increase --timeout",
    "network_dns": "Check DNS resolution and proxy settings",
    "network_tls": "Check the system clock, certificates and HTTPS proxy",
    "network_connect": "Check network and proxy connectivity",
    "snapshot_changed": "The leaderboard moved during pagination; retrying a complete snapshot",
    "api_error": "Check the API code and request ID in diagnostics",
    "schema_error": "Inspect this snapshot and its diagnostics; the last valid comparison is preserved",
}


def event(name: str, message: str, level: str = "INFO", **fields) -> None:
    LOGGER.log(getattr(logging, level), message, extra={"event_name": name, "fields": fields})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                 "level": record.levelname, "event": getattr(record, "event_name", "message"),
                 "message": record.getMessage(), "pid": record.process, "thread": record.threadName,
                 "version": __version__, **getattr(record, "fields", {})}
        if record.exc_info and record.exc_info[1]:
            value["exception"] = exception_details(record.exc_info[1])
        return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))


def human_line(value: dict) -> str:
    context = f" [{value['ranking']}]" if value.get("ranking") else ""
    context += f" #{value['poll_id']}" if value.get("poll_id") is not None else ""
    parts = [f"{value.get('time', '')} {value.get('level', 'INFO')}{context} {value.get('message', '')}"]
    for key in ("category", "http_status", "duration_ms", "consecutive_failures", "retry_in_seconds", "next_attempt_at"):
        if value.get(key) is not None:
            parts.append(f"{key}={value[key]}")
    if value.get("action"):
        parts.append(value["action"])
    if value.get("diagnostics"):
        parts.append(json.dumps(value["diagnostics"], ensure_ascii=False, separators=(",", ":")))
    if value.get("exception"):
        parts.append(json.dumps(value["exception"], ensure_ascii=False, separators=(",", ":")))
    return " | ".join(parts)


class HumanFormatter(JsonFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return human_line(json.loads(super().format(record)))


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def handleError(self, record):
        # Logging failures must not stop collection or dump a record containing secrets.
        if not getattr(self, "_warned", False):
            sys.stderr.write("BeauClaw could not write a log file; check disk space and permissions.\n")
            self._warned = True


@contextmanager
def log_session(db: Path, level: str = "INFO", *, console: bool = True,
                max_bytes: int = LOG_BYTES, backups: int = LOG_BACKUPS):
    path = db.with_suffix(".log")
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_level, previous_hook = LOGGER.level, threading.excepthook
    handlers = []
    try:
        for destination, threshold in ((path, getattr(logging, level)), (db.with_suffix(".errors.log"), logging.WARNING)):
            handler = PrivateRotatingHandler(destination, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            handler.setLevel(threshold)
            handler.setFormatter(JsonFormatter())
            handlers.append(handler)
            LOGGER.addHandler(handler)
        if console:
            handler = logging.StreamHandler()
            handler.setLevel(getattr(logging, level))
            handler.setFormatter(HumanFormatter())
            handlers.append(handler)
            LOGGER.addHandler(handler)
        LOGGER.setLevel(min(getattr(logging, level), logging.WARNING))

        def thread_error(args):
            event("thread.crashed", "Background thread stopped unexpectedly", "ERROR",
                  exception=exception_details(args.exc_value), worker=args.thread.name if args.thread else None)
        threading.excepthook = thread_error
        yield
    finally:
        threading.excepthook = previous_hook
        for handler in handlers:
            LOGGER.removeHandler(handler)
            handler.close()
        LOGGER.setLevel(previous_level)


def log_files(path: Path) -> list[Path]:
    archived = []
    for candidate in path.parent.glob(path.name + ".*"):
        suffix = candidate.name[len(path.name) + 1:]
        if suffix.isdigit():
            archived.append((int(suffix), candidate))
    return [p for _, p in sorted(archived, reverse=True)] + [path]


def read_logs(path: Path, *, lines: int = 50, follow: bool = False, ranking: str | None = None,
              level: str = "DEBUG", stop: threading.Event | None = None):
    """Filter before tailing; follow file replacement without replaying old records."""
    threshold = getattr(logging, level)

    def decode(line):
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        if not isinstance(value, dict) or "message" not in value:
            if ranking or threshold > logging.INFO:
                return None
            return {"time": "", "level": "INFO", "event": "legacy", "message": safe_text(line.rstrip())}
        if ranking and value.get("ranking") != ranking:
            return None
        severity = logging.getLevelNamesMapping().get(str(value.get("level")), logging.INFO)
        if severity < threshold:
            return None
        return sanitize(value)

    tail, current = deque(maxlen=lines), None
    try:
        for candidate in log_files(path):
            try:
                stream = candidate.open(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith("\n") and candidate == path and follow:
                    stream.seek(position)
                    break
                value = decode(line)
                if value is not None:
                    tail.append(value)
            if candidate == path:
                current = stream
            else:
                stream.close()
        yield from tail
        if not follow:
            return
        stop = stop or threading.Event()
        while not stop.is_set():
            if current is None:
                try:
                    current = path.open(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    stop.wait(0.2)
                    continue
            position = current.tell()
            line = current.readline()
            if line:
                # Do not emit half-written JSON records; the writer flushes each line.
                if not line.endswith("\n"):
                    try:
                        stat, opened = path.stat(), os.fstat(current.fileno())
                        if (stat.st_dev, stat.st_ino) != (opened.st_dev, opened.st_ino):
                            current.close()
                            current = None
                            continue  # A crash left a partial final line in the archive.
                    except FileNotFoundError:
                        pass
                    current.seek(position)
                    stop.wait(0.2)
                    continue
                value = decode(line)
                if value is not None:
                    yield value
                continue
            try:
                stat = path.stat()
                opened = os.fstat(current.fileno())
                if stat.st_ino != opened.st_ino or stat.st_dev != opened.st_dev:
                    current.close()
                    current = None
                    continue
                if stat.st_size < current.tell():
                    current.seek(0)
                    continue
            except FileNotFoundError:
                pass
            stop.wait(0.2)
    finally:
        if current:
            current.close()
