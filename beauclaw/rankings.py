"""Persistent ranking subscriptions and their independent history databases."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen

from beauclaw.core import API_ORIGIN, DEFAULT_COMPETITION, Store, competition_id, short_code, utcnow

DEFAULT_TITLE = "2026年CANN挑战赛_西北赛区"


def competition_title(event_id: str) -> str:
    # Public metadata, independent of the leaderboard's authenticated session.
    request = Request(f"{API_ORIGIN}/aihub/api/v1/activity/{event_id}?__s=compe",
                      headers={"User-Agent": "Mozilla/5.0 BeauClaw", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            value = json.loads(response.read(2 * 1024 * 1024))
        if isinstance(value.get("data"), dict):
            value = value["data"]
        name = value.get("activity_name") or value.get("name") or value.get("title")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return DEFAULT_TITLE if event_id == DEFAULT_COMPETITION else f"Competition {event_id}"


class Rankings:
    def __init__(self, db: Path):
        self.path = db.expanduser().resolve()
        self.store = Store(self.path)
        self.db = self.store.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS rankings (
            short_id TEXT PRIMARY KEY, competition_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, url TEXT NOT NULL, storage TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if "generation" not in {row[1] for row in self.db.execute("PRAGMA table_info(rankings)")}:
                self.db.execute("ALTER TABLE rankings ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
        if not self.db.execute("SELECT 1 FROM meta WHERE key='ranking_registry_initialized'").fetchone():
            previous = self.db.execute("SELECT value FROM meta WHERE key='competition_id'").fetchone()
            if previous:
                self.add(previous[0], name=DEFAULT_TITLE if previous[0] == DEFAULT_COMPETITION else None, legacy=True)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('ranking_registry_initialized','1')")

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def row(self, value) -> dict:
        result = dict(value)
        result["db"] = str(self.path.parent / result["storage"]) if result["storage"] else str(self.path)
        return result

    def list(self, include_deleted: bool = False) -> list[dict]:
        condition = "" if include_deleted else " WHERE active=1"
        return [self.row(row) for row in self.db.execute("SELECT * FROM rankings" + condition + " ORDER BY created_at,short_id")]

    def get(self, code: str) -> dict:
        row = self.db.execute("SELECT * FROM rankings WHERE short_id=? AND active=1", (code.lower(),)).fetchone()
        if row is None:
            raise ValueError("Ranking ID not found; run beauclaw ranking list")
        return self.row(row)

    def add(self, url: str, name: str | None = None, legacy: bool = False) -> dict:
        event_id = competition_id(url)
        canonical = f"https://competition.gitcode.com/competition/{event_id}/live-ranking"
        existing = self.db.execute("SELECT * FROM rankings WHERE competition_id=?", (event_id,)).fetchone()
        if existing:
            with self.db:
                self.db.execute("UPDATE rankings SET generation=generation+CASE WHEN active=0 THEN 1 ELSE 0 END,active=1 WHERE competition_id=?", (event_id,))
            return self.get(existing["short_id"])
        title = name or competition_title(event_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute("SELECT * FROM rankings WHERE competition_id=?", (event_id,)).fetchone()
            if existing:
                self.db.execute("UPDATE rankings SET generation=generation+CASE WHEN active=0 THEN 1 ELSE 0 END,active=1 WHERE competition_id=?", (event_id,))
                return self.get(existing["short_id"])
            code = short_code(canonical, {row[0] for row in self.db.execute("SELECT short_id FROM rankings")})
            self.db.execute("INSERT INTO rankings(short_id,competition_id,name,url,storage,active,created_at) VALUES (?,?,?,?,?,?,?)",
                            (code, event_id, title, canonical,
                             "" if legacy else f"rankings/{code}.sqlite3", 1, utcnow()))
        return self.get(code)

    def delete(self, code: str) -> dict:
        row = self.get(code)
        with self.db:
            self.db.execute("UPDATE rankings SET active=0,generation=generation+1 WHERE short_id=?", (row["short_id"],))
        self.cancel_pending(Path(row["db"]))
        return row

    @staticmethod
    def cancel_pending(db: Path, recipient: str | None = None):
        if not db.is_file():
            return
        connection = sqlite3.connect(db, timeout=10)
        try:
            with connection:
                query = "UPDATE mail_outbox SET cancelled_at=? WHERE sent_at IS NULL AND cancelled_at IS NULL"
                connection.execute(query + (" AND recipient=?" if recipient else ""),
                                   (utcnow(), recipient) if recipient else (utcnow(),))
        finally:
            connection.close()

    def cancel_recipient(self, recipient: str):
        for row in self.list(include_deleted=True):
            self.cancel_pending(Path(row["db"]), recipient)


def is_active(db: Path, code: str, generation: int | None = None) -> bool:
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        row = connection.execute("SELECT active,generation FROM rankings WHERE short_id=?", (code,)).fetchone()
        return bool(row and row[0] and (generation is None or row[1] == generation))
    finally:
        connection.close()


def ranking_status(db: Path) -> list[dict]:
    if not db.is_file():
        return []
    result = []
    with Rankings(db) as registry:
        for row in registry.list():
            summary = None
            if Path(row["db"]).is_file():
                history = Store(Path(row["db"]), notice_db=db)
                try:
                    summary = history.summary()
                    summary.pop("states")
                    summary.pop("kinds")
                finally:
                    history.close()
            result.append({**row, "observations": summary})
    return result
