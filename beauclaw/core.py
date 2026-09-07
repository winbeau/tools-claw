"""Persist leaderboard observations, with GitCode parsing and shared state logic."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_COMPETITION = "2094722369343447042"
SNAPSHOT_LIMIT = 100
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


def should_notify_leader(event: dict) -> bool:
    before, after = event["before"], event["after"]
    return (before.get("key") != after.get("key") or before["name"] != after["name"]
            or Decimal(after["score"]) > Decimal(before["score"]))


def competition_id(value: str) -> str:
    import re
    match = re.fullmatch(r"(?:https://competition\.gitcode\.com/competition/)?(\d+)(?:/live-ranking/?(?:\?[^#]*)?)?", value)
    if not match:
        raise ValueError("Provide a numeric competition ID or a GitCode live-ranking URL")
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
    return fetch_url(url, headers, timeout)


def fetch_url(url: str, headers: dict, timeout: float = 8) -> Response:
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
                                error="Response exceeds 20 MiB; truncated data was not compared")
            return Response(started, utcnow(), url, response.code, body, safe_headers)
    except (URLError, OSError, ValueError) as exc:
        return Response(started, utcnow(), url, None, error=f"Network request failed: {type(exc).__name__}")


class InvalidBoard(ValueError):
    pass


def score_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise InvalidBoard("Leaderboard contains a missing or invalid score")
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
        raise InvalidBoard("Leaderboard contains an unparseable score") from None


def format_score(value: Any) -> str:
    """Round only the displayed score; stored scores keep their original precision."""
    score = score_text(value)
    with localcontext() as context:
        context.prec = max(28, len(score) + 4)
        rounded = Decimal(score).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return "0.000" if rounded == 0 else format(rounded, "f")


def parse_board(body: bytes, observed_at: str) -> dict:
    try:
        payload = json.loads(body, parse_float=str)
    except (ValueError, UnicodeError):
        raise InvalidBoard("Response is not valid JSON; it may be a login or block page") from None
    if not isinstance(payload, dict):
        raise InvalidBoard("Unexpected response structure; expected a leaderboard object")
    for field_name in ("error_code", "code"):
        if field_name in payload and str(payload[field_name]) not in ("0", "200", "None"):
            raise InvalidBoard(f"API returned a business error {field_name}={payload[field_name]}")
    if "data" in payload and "current_schedule" not in payload:
        payload = payload["data"]
    if not isinstance(payload, dict) or "current_schedule" not in payload:
        raise InvalidBoard("Response has no current_schedule and was not compared")
    schedule = payload["current_schedule"]
    if schedule is None:
        return {"status": "unavailable", "reason": "No schedule is currently available", "boards": {}}
    if not isinstance(schedule, dict) or schedule.get("id") is None:
        raise InvalidBoard("Schedule has no ID; different schedules cannot be separated safely")
    result = {"status": "ok", "schedule_id": str(schedule["id"]),
              "schedule_name": str(schedule.get("name") or schedule["id"]), "boards": {}}
    if schedule.get("seal_time"):
        try:
            seal_time = datetime.fromisoformat(str(schedule["seal_time"]).replace("Z", "+00:00"))
            # GitCode's timezone-less competition timestamps use China Standard Time.
            if seal_time.tzinfo is None:
                seal_time = seal_time.replace(tzinfo=timezone(timedelta(hours=8)))
            if datetime.fromisoformat(observed_at) >= seal_time:
                return {**result, "status": "sealed", "reason": "The current schedule is sealed"}
        except ValueError:
            raise InvalidBoard("Invalid seal time; response was not compared") from None
    for board_key in BOARDS:
        rows = payload.get(board_key)
        if not isinstance(rows, list):
            raise InvalidBoard(f"Response lacks a complete {board_key} array and was not compared")
        members = {}
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise InvalidBoard("Unexpected leaderboard row structure")
            name = str(row.get("team_name") or "").strip()
            if not name or "score" not in row:
                raise InvalidBoard("Leaderboard row lacks a team name or score and was not compared")
            stable_field = next((key for key in ("team_id", "namespace_id")
                                 if row.get(key) is not None and str(row[key]) != ""), None)
            key = f"{stable_field}:{row[stable_field]}" if stable_field else f"name:{name}"
            if key in members:
                raise InvalidBoard("Duplicate team identities; response was not compared")
            members[key] = {"key": key, "name": name, "rank": rank,
                            "score": score_text(row["score"]),
                            "identity_basis": stable_field or "team_name"}
        result["boards"][board_key] = members
    if not any(result["boards"].values()):
        result.update(status="unavailable", reason="Both boards are empty; preserving the previous valid snapshot")
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
            emit("board_empty", message="Board is empty; retaining previous entries without marking teams missing")
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
    def __init__(self, path: Path, event_id: str | None = None, notice_db: Path | None = None,
                 provider: str | None = None):
        from beauclaw.providers import get_provider
        self.path = Path(path)
        self.notice_db = Path(notice_db) if notice_db else self.path
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
                error TEXT, critical INTEGER NOT NULL DEFAULT 0, critical_reason TEXT);
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
        saved_provider = self.db.execute("SELECT value FROM meta WHERE key='provider'").fetchone()
        existing = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
        bound_provider = saved_provider[0] if saved_provider else "gitcode" if existing else None
        if (event_id and existing and existing[0] != event_id) or (provider and bound_provider and provider != bound_provider):
            self.close()
            raise ValueError("Database belongs to another competition or provider; use a different --db path")
        self.source = get_provider(provider or bound_provider or "gitcode")
        self.provider = self.source.id
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if event_id:
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('competition_id', ?)", (event_id,))
            if provider or existing or event_id:
                self.db.execute("INSERT OR IGNORE INTO meta VALUES ('provider', ?)", (self.provider,))
            if "short_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(notices)")}:
                self.db.execute("ALTER TABLE notices ADD COLUMN short_id TEXT")
            used = {row[0] for row in self.db.execute("SELECT short_id FROM notices WHERE short_id IS NOT NULL")}
            for row in self.db.execute("SELECT id,email FROM notices WHERE short_id IS NULL").fetchall():
                code = short_code(row["email"], used)
                used.add(code)
                self.db.execute("UPDATE notices SET short_id=? WHERE id=?", (code, row["id"]))
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS notices_short_id ON notices(short_id)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(polls)")}
            if "critical" not in columns:
                self.db.execute("ALTER TABLE polls ADD COLUMN critical INTEGER NOT NULL DEFAULT 0")
            if "critical_reason" not in columns:
                self.db.execute("ALTER TABLE polls ADD COLUMN critical_reason TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS polls_critical_id ON polls(critical,id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS polls_body ON polls(body_sha256)")
            if not self.db.execute("SELECT 1 FROM meta WHERE key='snapshot_retention_v1'").fetchone():
                # Protect historical evidence before the first automatic cleanup, even
                # if mail was disabled or there were no recipients at the time.
                for row in self.db.execute("SELECT poll_id,details_json FROM events WHERE kind='leader_changed' AND scope LIKE ?",
                                           ("%:" + self.source.primary_board,)).fetchall():
                    self._mark_critical(row["poll_id"], "leader_changed")
                    self._mark_critical(json.loads(row["details_json"]).get("previous_poll_id"), "before_leader_change")
                for row in self.db.execute("SELECT poll_id,payload_json FROM mail_outbox").fetchall():
                    self._mark_critical(row["poll_id"], "mail_evidence")
                    for event in json.loads(row["payload_json"]).get("events", []):
                        self._mark_critical(event.get("details", {}).get("previous_poll_id"), "before_leader_change")
                self.db.execute("INSERT INTO meta VALUES ('snapshot_retention_v1','1')")

    def close(self) -> None:
        self.db.close()

    def _mark_critical(self, poll_id: int | None, reason: str) -> None:
        self.db.execute("""UPDATE polls SET critical=1,critical_reason=?
            WHERE id=? AND (critical=0 OR ?='leader_changed')""", (reason, poll_id, reason))

    def _prune_snapshots(self) -> None:
        # Keep live comparison baselines even across extended outages and schedule
        # switches, so a future change can still protect its preceding raw snapshot.
        anchors = {row[0] for row in self.db.execute("SELECT max(id) FROM polls") if row[0] is not None}
        for row in self.db.execute("SELECT state_json FROM states"):
            poll_id = json.loads(row[0]).get("last_valid_poll_id")
            if poll_id is not None:
                anchors.add(poll_id)
        normal = {row[0]: row[1] for row in self.db.execute("SELECT id,body_sha256 FROM polls WHERE critical=0 ORDER BY id DESC")}
        if len(normal) <= SNAPSHOT_LIMIT:
            return
        keep = anchors.intersection(normal)
        for poll_id in normal:
            if len(keep) >= SNAPSHOT_LIMIT:
                break
            keep.add(poll_id)
        expired = {poll_id: sha for poll_id, sha in normal.items() if poll_id not in keep}
        self.db.executemany("DELETE FROM polls WHERE id=? AND critical=0", ((poll_id,) for poll_id in expired))
        # Bodies may be shared by many captures. Remove only unreferenced bodies;
        # event history, states and pending/sent mail are never pruned.
        self.db.executemany("DELETE FROM bodies WHERE sha256=? AND NOT EXISTS (SELECT 1 FROM polls WHERE body_sha256=bodies.sha256)",
                            ((sha,) for sha in set(expired.values())))

    def record(self, response: Response, missing_samples: int = 2,
               mail_policy: dict | None = None) -> tuple[dict, list]:
        error = response.error
        info: dict = {}
        if error:
            status = "error"
        elif response.status != 200:
            status = "error"
            error = {401: "Session missing or expired; run beauclaw login",
                     403: "API access denied", 418: "Request blocked by the site", 429: "API rate limit reached; retrying later"}.get(
                         response.status, f"API HTTP {response.status}")
        else:
            try:
                info = self.source.parse(response.body, response.captured_at)
                bound_id = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
                if info.get("competition_id") and bound_id and info["competition_id"] != bound_id[0]:
                    raise InvalidBoard("Leaderboard response belongs to another competition; it was not compared")
                info.setdefault("primary_board", self.source.primary_board)
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
                               "title": self.source.boards[board_key]}
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
            critical = False
            for event in events:
                if event["kind"] == "leader_changed" and event["scope"].endswith(":" + self.source.primary_board):
                    critical = True
                    self._mark_critical(poll_id, "leader_changed")
                    self._mark_critical(event["details"].get("previous_poll_id"), "before_leader_change")
            if mail_policy:
                leaders = [e for e in events if e["kind"] == "leader_changed"
                           and e["scope"].split(":", 1)[1] in mail_policy["boards"]
                           and should_notify_leader(e)]
                if leaders:
                    event_id = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()[0]
                    for notice in self.notices():
                        message = {"competition_id": event_id, "provider": self.provider,
                                   "source_url": self.source.url(event_id), "captured_at": response.captured_at,
                                   "poll_id": poll_id, "events": leaders, "critical": True, "sender": mail_policy["sender"],
                                   "recipient": notice["email"], "recipient_id": notice["id"],
                                   "recipient_created_at": notice["created_at"],
                                   "schedule_name": info["schedule_name"],
                                   "competition_name": mail_policy.get("competition_name") or info.get("competition_name") or "CANN 挑战赛",
                                   "ranking_id": mail_policy.get("ranking_id"),
                                   "ranking_generation": mail_policy.get("ranking_generation"),
                                   "top10": top_ten(info)}
                        self.db.execute("INSERT INTO mail_outbox (poll_id,recipient,message_id,payload_json) VALUES (?,?,?,?)",
                                        (poll_id, notice["email"], f"<{uuid.uuid4().hex}@beauclaw.local>", dumps(message)))
            self._prune_snapshots()
        return {"id": poll_id, "status": status, "error": error, "captured_at": response.captured_at,
                "info": metadata, "http_status": response.status, "critical": critical}, events

    def summary(self) -> dict:
        def poll(row: Any) -> dict | None:
            if row is None:
                return None
            value = dict(row)
            value["critical"] = bool(value["critical"])
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
            return {"competition_id": event_id[0] if event_id else None, "provider": self.provider,
                    "provider_name": self.source.name, "primary_board": self.source.primary_board,
                    "source_url": self.source.url(event_id[0]) if event_id else None, "latest": latest,
                    "last_valid": good, "states": states, "kinds": KINDS,
                    "interval_seconds": float(interval[0]) if interval else 10,
                    "mail_pending": self.db.execute("SELECT count(*) FROM mail_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL").fetchone()[0],
                    "mail_sent": self.db.execute("SELECT count(*) FROM mail_outbox WHERE sent_at IS NOT NULL").fetchone()[0],
                    "notice_count": len(self.notices()),
                    "mail_last_error": (lambda r: r[0] if r else None)(self.db.execute(
                        "SELECT last_error FROM mail_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND last_error IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()),
                    "poll_count": self.db.execute("SELECT count(*) FROM polls").fetchone()[0],
                    "critical_poll_count": self.db.execute("SELECT count(*) FROM polls WHERE critical=1").fetchone()[0],
                    "snapshot_limit": SNAPSHOT_LIMIT,
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
            snapshot = self.db.execute("SELECT critical FROM polls WHERE id=?", (event["poll_id"],)).fetchone()
            event["critical"] = bool(snapshot and snapshot[0])
            event["snapshot_available"] = snapshot is not None
            for field, poll_id in (("previous_snapshot_available", event["details"].get("previous_poll_id")),
                                   ("last_seen_snapshot_available", event["details"].get("last_seen_poll_id"))):
                event[field] = self.db.execute("SELECT 1 FROM polls WHERE id=?", (poll_id,)).fetchone() is not None
            result.append(event)
        return result

    def notices(self) -> list[dict]:
        if self.notice_db.resolve() != self.path.resolve():
            connection = sqlite3.connect(self.notice_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                return [dict(row) for row in connection.execute("SELECT * FROM notices ORDER BY id")]
            finally:
                connection.close()
        return [dict(row) for row in self.db.execute("SELECT * FROM notices ORDER BY id")]

    def add_notice(self, email: str) -> bool:
        from beauclaw.mail import address
        email = address(email.strip()).lower()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM notices WHERE email=?", (email,)).fetchone():
                return False
            code = short_code(email, {row[0] for row in self.db.execute("SELECT short_id FROM notices")})
            result = self.db.execute("INSERT INTO notices(email,created_at,short_id) VALUES (?,?,?)", (email, utcnow(), code))
            return result.rowcount == 1

    def delete_notice(self, key: str) -> str:
        with self.db:
            row = self.db.execute("SELECT * FROM notices WHERE short_id=?", (key.strip().lower(),)).fetchone()
            if row is None:
                row = self.db.execute("SELECT * FROM notices WHERE email=? OR CAST(id AS TEXT)=?",
                                      (key.strip().lower(), key.strip())).fetchone()
            if row is None:
                raise ValueError("Recipient ID not found; run beauclaw notice list")
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
        result["critical"] = bool(result["critical"])
        result["raw_body"] = zlib.decompress(result.pop("compressed")).decode("utf-8", errors="replace")
        for key in ("headers", "info"):
            result[key] = json.loads(result.pop(f"{key}_json"))
        return result

    def ranking_snapshot(self, poll_id: int | None = None) -> dict:
        if poll_id is None:
            row = self.db.execute("SELECT id FROM polls WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return {}
            poll_id = row[0]
        snapshot = self.snapshot(poll_id)
        if not snapshot:
            return {}
        try:
            board = self.source.parse(snapshot["raw_body"].encode(), snapshot["captured_at"])
        except InvalidBoard:
            return {}
        event_id = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
        return {"poll_id": poll_id, "provider": self.provider, "captured_at": snapshot["captured_at"],
                "competition_id": event_id[0] if event_id else DEFAULT_COMPETITION,
                "schedule_name": board.get("schedule_name", ""), "top10": top_ten(board)}


def top_ten(board: dict) -> list[dict]:
    members = board.get("boards", {}).get(board.get("primary_board", "realtime_region_ranking"), {})
    return [{key: member[key] for key in ("rank", "name", "score", "organization", "display_score", "display_rank") if key in member}
            for member in sorted(members.values(), key=lambda row: row["rank"])[:10]]


def short_code(identity: str, used: set[str]) -> str:
    for salt in range(1_000_000):
        value = identity if salt == 0 else f"{identity}\0{salt}"
        code = hashlib.sha256(value.encode()).hexdigest()[:6]
        if code not in used:
            return code
    raise ValueError("Unable to allocate a unique six-character ID")
