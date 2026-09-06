"""Fetch, validate, and persist public CANN leaderboard observations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_COMPETITION = "2094722369343447042"
API_ORIGIN = "https://web-api.gitcode.com"
BOARDS = {
    "realtime_region_ranking": "参赛区域实时总榜",
    "realtime_all_member_ranking": "实时总榜",
}
KINDS = {
    "baseline": "建立基线", "appeared": "首次出现", "returned": "重新出现",
    "score_up": "分数上升", "score_drop": "分数下降", "rank_changed": "排名变化",
    "renamed": "名称变化", "missing": "暂未出现", "missing_confirmed": "持续未出现",
    "board_empty": "榜单为空", "board_resumed": "榜单恢复",
    "leader_changed": "榜一变化",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def competition_id(value: str) -> str:
    import re
    match = re.fullmatch(r"(?:https://competition\.gitcode\.com/competition/)?(\d+)(?:/live-ranking/?(?:\?[^#]*)?)?", value)
    if not match:
        raise ValueError("请提供赛事数字 ID 或 GitCode 实时榜单链接")
    return match[1]


def api_url(event_id: str) -> str:
    return f"{API_ORIGIN}/aihub/api/v1/activity/{event_id}/cann_leaderboard?__s=compe"


@dataclass
class Response:
    started_at: str
    captured_at: str
    url: str
    status: int | None
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str | None = None


def fetch(event_id: str, auth: dict, timeout: float = 8) -> Response:
    url = api_url(event_id)
    headers = {
        "User-Agent": "Mozilla/5.0 BeauClaw/0.1",
        "Accept": "application/json", "Cache-Control": "no-cache",
        "Referer": f"https://competition.gitcode.com/competition/{event_id}/live-ranking",
        "Origin": "https://competition.gitcode.com",
    }
    if auth.get("token"):
        token = auth["token"].strip()
        headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
    if auth.get("cookie"):
        headers["Cookie"] = auth["cookie"]
    started = utcnow()
    try:
        try:
            response = urlopen(Request(url, headers=headers), timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            # Retain evidence headers only, never Set-Cookie or request credentials.
            safe_headers = {k.lower(): v for k, v in response.headers.items()
                            if k.lower() in {"date", "content-type", "etag", "last-modified",
                                             "age", "retry-after", "cache-control", "x-request-id"}}
            body = response.read(20 * 1024 * 1024 + 1)
            if len(body) > 20 * 1024 * 1024:
                return Response(started, utcnow(), url, response.code, headers=safe_headers,
                                error="响应超过 20 MiB，未将截断数据用于比较")
            return Response(started, utcnow(), url, response.code, body, safe_headers)
    except (URLError, OSError, ValueError) as exc:
        return Response(started, utcnow(), url, None, error=f"网络请求失败：{type(exc).__name__}")


class InvalidBoard(ValueError):
    pass


def score_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise InvalidBoard("榜单包含缺失或非法分数")
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        # Do not use normalize(): it can round values to the Decimal context precision.
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if number == 0 else text
    except (InvalidOperation, ValueError):
        raise InvalidBoard("榜单包含无法解析的分数") from None


def parse_board(body: bytes, observed_at: str) -> dict:
    try:
        payload = json.loads(body, parse_float=str)
    except (ValueError, UnicodeError):
        raise InvalidBoard("响应不是有效 JSON，可能是登录页或拦截页") from None
    if not isinstance(payload, dict):
        raise InvalidBoard("响应结构变化：预期为榜单对象")
    for field_name in ("error_code", "code"):
        if field_name in payload and str(payload[field_name]) not in ("0", "200", "None"):
            raise InvalidBoard(f"接口返回业务错误 {field_name}={payload[field_name]}")
    if "data" in payload and "current_schedule" not in payload:
        payload = payload["data"]
    if not isinstance(payload, dict) or "current_schedule" not in payload:
        raise InvalidBoard("响应缺少 current_schedule，未用于比较")
    schedule = payload["current_schedule"]
    if schedule is None:
        return {"status": "unavailable", "reason": "当前没有可用赛程", "boards": {}}
    if not isinstance(schedule, dict) or schedule.get("id") is None:
        raise InvalidBoard("赛程缺少 ID，无法安全区分不同赛程")
    result = {"status": "ok", "schedule_id": str(schedule["id"]),
              "schedule_name": str(schedule.get("name") or schedule["id"]), "boards": {}}
    if schedule.get("seal_time"):
        try:
            seal_time = datetime.fromisoformat(str(schedule["seal_time"]).replace("Z", "+00:00"))
            # GitCode's timezone-less competition timestamps use China Standard Time.
            if seal_time.tzinfo is None:
                seal_time = seal_time.replace(tzinfo=timezone(timedelta(hours=8)))
            if datetime.fromisoformat(observed_at) >= seal_time:
                return {**result, "status": "sealed", "reason": "当前赛程已封榜"}
        except ValueError:
            raise InvalidBoard("无法解析封榜时间，未用于比较") from None
    for board_key in BOARDS:
        rows = payload.get(board_key)
        if not isinstance(rows, list):
            raise InvalidBoard(f"响应缺少完整的 {board_key} 数组，未用于比较")
        members = {}
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise InvalidBoard("榜单行结构异常")
            name = str(row.get("team_name") or "").strip()
            if not name or "score" not in row:
                raise InvalidBoard("榜单行缺少队名或分数，未用于比较")
            stable_field = next((key for key in ("team_id", "namespace_id")
                                 if row.get(key) is not None and str(row[key]) != ""), None)
            key = f"{stable_field}:{row[stable_field]}" if stable_field else f"name:{name}"
            if key in members:
                raise InvalidBoard("榜单含重复队伍标识，未用于比较")
            members[key] = {"key": key, "name": name, "rank": rank,
                            "score": score_text(row["score"]),
                            "identity_basis": stable_field or "team_name"}
        result["boards"][board_key] = members
    if not any(result["boards"].values()):
        result.update(status="unavailable", reason="两个榜单均为空，保留上一份有效记录")
    return result


def update_state(state: dict | None, members: dict, context: dict,
                 observed_at: str, poll_id: int, missing_samples: int) -> tuple[dict, list]:
    events: list[dict] = []
    baseline = state is None or not state["members"]
    if state is None:
        state = {**context, "members": {}, "available": True}

    def emit(kind: str, key: str = "", before: dict | None = None,
             after: dict | None = None, **details: Any) -> None:
        events.append({"kind": kind, "member_key": key,
                       "name": (after or before or {}).get("name", ""),
                       "before": before, "after": after, "details": details})

    state["latest_poll_id"] = poll_id
    if not members:
        if state["available"]:
            emit("board_empty", message="整榜为空，保留上次数据，不判定队伍消失")
        state["available"] = False
        return state, events
    if not state["available"]:
        emit("board_resumed")
    state["available"] = True
    state["last_valid_at"] = observed_at
    if baseline:
        emit("baseline", count=len(members))
    else:
        previous_leader = next((m["entry"] for m in state["members"].values()
                                if m["present"] and m["entry"]["rank"] == 1), None)
        current_leader = next(m for m in members.values() if m["rank"] == 1)
        if previous_leader and any(previous_leader[k] != current_leader[k] for k in ("key", "name", "score")):
            emit("leader_changed", current_leader["key"], previous_leader, current_leader,
                 previous_poll_id=state.get("last_valid_poll_id"))
    for key, entry in members.items():
        old = state["members"].get(key)
        if old is None:
            if not baseline:
                emit("appeared", key, after=entry)
            old = {"entry": entry, "first_seen": observed_at, "peak_score": entry["score"],
                   "peak_at": observed_at, "best_rank": entry["rank"]}
            state["members"][key] = old
        else:
            previous = old["entry"]
            if not old["present"]:
                emit("returned", key, previous, entry, missing_since=old["missing_since"])
            if previous["name"] != entry["name"]:
                emit("renamed", key, previous, entry)
            current_score, previous_score = Decimal(entry["score"]), Decimal(previous["score"])
            if current_score != previous_score:
                emit("score_up" if current_score > previous_score else "score_drop", key,
                     previous, entry, historical_peak=old["peak_score"])
            elif previous["rank"] != entry["rank"]:
                emit("rank_changed", key, previous, entry)
            if current_score > Decimal(old["peak_score"]):
                old.update(peak_score=entry["score"], peak_at=observed_at)
            old["best_rank"] = min(old["best_rank"], entry["rank"])
        old.update(entry=entry, last_seen=observed_at, last_seen_poll_id=poll_id,
                   present=True, missing_count=0, missing_since=None, missing_announced=False)
    for key, old in state["members"].items():
        if key in members:
            continue
        if old["present"]:
            emit("missing", key, old["entry"], last_seen=old["last_seen"],
                 last_seen_poll_id=old["last_seen_poll_id"])
            old.update(present=False, missing_since=observed_at)
        old["missing_count"] += 1
        if old["missing_count"] >= missing_samples and not old["missing_announced"]:
            emit("missing_confirmed", key, old["entry"], samples=old["missing_count"],
                 missing_since=old["missing_since"], last_seen=old["last_seen"],
                 last_seen_poll_id=old["last_seen_poll_id"])
            old["missing_announced"] = True
    state["last_valid_poll_id"] = poll_id
    return state, events


class Store:
    def __init__(self, path: Path, event_id: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bodies (sha256 TEXT PRIMARY KEY, compressed BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, captured_at TEXT NOT NULL,
                url TEXT NOT NULL, http_status INTEGER, status TEXT NOT NULL,
                body_sha256 TEXT NOT NULL, headers_json TEXT NOT NULL, info_json TEXT NOT NULL,
                error TEXT);
            CREATE TABLE IF NOT EXISTS states (scope TEXT PRIMARY KEY, state_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, poll_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                scope TEXT NOT NULL, kind TEXT NOT NULL, member_key TEXT NOT NULL,
                name TEXT NOT NULL, before_json TEXT, after_json TEXT, details_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_kind_id ON events(kind, id);
            CREATE INDEX IF NOT EXISTS polls_status_id ON polls(status, id);
            CREATE TABLE IF NOT EXISTS mail_outbox (
                id INTEGER PRIMARY KEY, poll_id INTEGER NOT NULL, recipient TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                sent_at TEXT, cancelled_at TEXT, last_error TEXT, UNIQUE(poll_id,recipient));
            CREATE INDEX IF NOT EXISTS mail_pending ON mail_outbox(sent_at, next_attempt);
            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL);
        """)
        if event_id:
            existing = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
            if existing and existing[0] != event_id:
                self.close()
                raise ValueError("数据库属于另一个赛事，请使用不同的 --db 路径")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('competition_id', ?)", (event_id,))

    def close(self) -> None:
        self.db.close()

    def record(self, response: Response, missing_samples: int = 2,
               mail_policy: dict | None = None) -> tuple[dict, list]:
        error = response.error
        info: dict = {}
        if error:
            status = "error"
        elif response.status != 200:
            status = "error"
            error = {401: "登录已失效或尚未登录，请运行 login 更新登录凭据",
                     403: "接口拒绝访问", 418: "请求被站点拦截", 429: "接口限流，稍后重试"}.get(
                         response.status, f"接口 HTTP {response.status}")
        else:
            try:
                info = parse_board(response.body, response.captured_at)
                status = info["status"]
            except InvalidBoard as exc:
                status, error = "error", str(exc)
        sha = hashlib.sha256(response.body).hexdigest()
        events = []
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO bodies VALUES (?, ?)", (sha, zlib.compress(response.body)))
            metadata = {key: value for key, value in info.items() if key != "boards"}
            metadata["counts"] = {key: len(value) for key, value in info.get("boards", {}).items()}
            cursor = self.db.execute("""INSERT INTO polls
                (started_at,captured_at,url,http_status,status,body_sha256,headers_json,info_json,error)
                VALUES (?,?,?,?,?,?,?,?,?)""", (response.started_at, response.captured_at, response.url,
                    response.status, status, sha, dumps(response.headers), dumps(metadata), error))
            poll_id = cursor.lastrowid
            if status == "ok":
                for board_key, members in info["boards"].items():
                    scope = f"{info['schedule_id']}:{board_key}"
                    row = self.db.execute("SELECT state_json FROM states WHERE scope=?", (scope,)).fetchone()
                    state = json.loads(row[0]) if row else None
                    context = {"scope": scope, "schedule_id": info["schedule_id"],
                               "schedule_name": info["schedule_name"], "board": board_key,
                               "title": BOARDS[board_key]}
                    state, new_events = update_state(state, members, context, response.captured_at,
                                                     poll_id, missing_samples)
                    self.db.execute("INSERT OR REPLACE INTO states VALUES (?,?)", (scope, dumps(state)))
                    for event in new_events:
                        self.db.execute("""INSERT INTO events
                            (poll_id,observed_at,scope,kind,member_key,name,before_json,after_json,details_json)
                            VALUES (?,?,?,?,?,?,?,?,?)""", (poll_id, response.captured_at, scope,
                                event["kind"], event["member_key"], event["name"], dumps(event["before"]),
                                dumps(event["after"]), dumps(event["details"])))
                        events.append({**event, "scope": scope, "poll_id": poll_id})
            if mail_policy:
                leaders = [e for e in events if e["kind"] == "leader_changed"
                           and e["scope"].split(":", 1)[1] in mail_policy["boards"]
                           and (mail_policy["notify_score"] or e["before"]["key"] != e["after"]["key"])]
                if leaders:
                    event_id = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()[0]
                    for notice in self.notices():
                        message = {"competition_id": event_id, "captured_at": response.captured_at,
                                   "poll_id": poll_id, "events": leaders, "sender": mail_policy["sender"],
                                   "recipient": notice["email"], "schedule_name": info["schedule_name"]}
                        self.db.execute("INSERT INTO mail_outbox (poll_id,recipient,message_id,payload_json) VALUES (?,?,?,?)",
                                        (poll_id, notice["email"], f"<{uuid.uuid4().hex}@beauclaw.local>", dumps(message)))
        return {"id": poll_id, "status": status, "error": error, "captured_at": response.captured_at,
                "info": metadata, "http_status": response.status}, events

    def summary(self) -> dict:
        def poll(row: Any) -> dict | None:
            if row is None:
                return None
            value = dict(row)
            value["info"] = json.loads(value.pop("info_json"))
            value.pop("headers_json")
            return value
        # Each dashboard refresh reads one consistent committed snapshot.
        self.db.execute("BEGIN")
        try:
            latest = poll(self.db.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone())
            good = poll(self.db.execute("SELECT * FROM polls WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone())
            states = [json.loads(row[0]) for row in self.db.execute("SELECT state_json FROM states")]
            event_id = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
            interval = self.db.execute("SELECT value FROM meta WHERE key='interval_seconds'").fetchone()
            return {"competition_id": event_id[0] if event_id else None, "latest": latest,
                    "last_valid": good, "states": states, "kinds": KINDS,
                    "interval_seconds": float(interval[0]) if interval else 10,
                    "mail_pending": self.db.execute("SELECT count(*) FROM mail_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL").fetchone()[0],
                    "mail_sent": self.db.execute("SELECT count(*) FROM mail_outbox WHERE sent_at IS NOT NULL").fetchone()[0],
                    "notice_count": self.db.execute("SELECT count(*) FROM notices").fetchone()[0],
                    "mail_last_error": (lambda r: r[0] if r else None)(self.db.execute(
                        "SELECT last_error FROM mail_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND last_error IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()),
                    "poll_count": self.db.execute("SELECT count(*) FROM polls").fetchone()[0],
                    "event_count": self.db.execute("SELECT count(*) FROM events").fetchone()[0]}
        finally:
            self.db.rollback()

    def events(self, limit: int = 200, before_id: int | None = None, signals_only: bool = False) -> list:
        condition = " AND kind IN ('score_drop','missing','missing_confirmed','returned','renamed','leader_changed')" if signals_only else ""
        rows = self.db.execute(f"SELECT * FROM events WHERE id < ?{condition} ORDER BY id DESC LIMIT ?",
                               (before_id or 9223372036854775807, limit))
        result = []
        for row in rows:
            event = dict(row)
            for key in ("before", "after", "details"):
                event[key] = json.loads(event.pop(f"{key}_json"))
            result.append(event)
        return result

    def notices(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM notices ORDER BY id")]

    def add_notice(self, email: str) -> bool:
        from beauclaw.mail import address
        email = address(email.strip()).lower()
        with self.db:
            result = self.db.execute("INSERT OR IGNORE INTO notices(email,created_at) VALUES (?,?)", (email, utcnow()))
            return result.rowcount == 1

    def delete_notice(self, key: str) -> str:
        with self.db:
            row = self.db.execute("SELECT * FROM notices WHERE email=? OR CAST(id AS TEXT)=?",
                                  (key.strip().lower(), key.strip())).fetchone()
            if row is None:
                raise ValueError("找不到该通知邮箱，请运行 beauclaw notice list")
            self.db.execute("DELETE FROM notices WHERE id=?", (row["id"],))
            self.db.execute("UPDATE mail_outbox SET cancelled_at=? WHERE recipient=? AND sent_at IS NULL",
                            (utcnow(), row["email"]))
            return row["email"]

    def snapshot(self, poll_id: int) -> dict | None:
        row = self.db.execute("""SELECT polls.*, bodies.compressed FROM polls
            JOIN bodies ON polls.body_sha256=bodies.sha256 WHERE polls.id=?""", (poll_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["raw_body"] = zlib.decompress(result.pop("compressed")).decode("utf-8", errors="replace")
        for key in ("headers", "info"):
            result[key] = json.loads(result.pop(f"{key}_json"))
        return result
