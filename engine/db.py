"""SQLite-хранилище: фото, посты, метрики, история плейбука.

SQLite на Railway Volume — проще и надёжнее отдельной базы для одного
сервиса с одним процессом. Все обращения идут через этот модуль.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,              -- library | before | ref | generated
    theme TEXT,
    title TEXT,
    description TEXT,
    used_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,            -- planned | rendered | pending | approved | rejected | publishing | published | failed
    format TEXT NOT NULL,
    hook_id TEXT NOT NULL,
    theme TEXT NOT NULL,
    explore INTEGER NOT NULL DEFAULT 0,
    script_json TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL DEFAULT '[]',
    video_path TEXT,
    cover_path TEXT,
    caption TEXT,
    media_token TEXT,
    approval_message_id INTEGER,
    scheduled_for TEXT,
    ig_container_id TEXT,
    ig_media_id TEXT,
    permalink TEXT,
    published_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
CREATE TABLE IF NOT EXISTS metrics (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    collected_at TEXT NOT NULL,
    plays INTEGER, reach INTEGER, likes INTEGER, comments INTEGER,
    saves INTEGER, shares INTEGER, avg_watch_ms INTEGER, total_watch_ms INTEGER,
    PRIMARY KEY (post_id, collected_at)
);
CREATE TABLE IF NOT EXISTS playbook_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    playbook_json TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid

    # ------------------------------------------------------------- assets
    def add_asset(self, path: str, kind: str, theme: str | None = None,
                  title: str | None = None, description: str | None = None) -> int:
        row = self.one("SELECT id FROM assets WHERE path=?", (path,))
        if row:
            return row["id"]
        return self.x(
            "INSERT INTO assets(path, kind, theme, title, description, created_at) VALUES (?,?,?,?,?,?)",
            (path, kind, theme, title, description, now_iso()),
        )

    def assets(self, kind: str, theme: str | None = None) -> list[sqlite3.Row]:
        if theme:
            return self.q("SELECT * FROM assets WHERE kind=? AND theme=? ORDER BY used_count, id", (kind, theme))
        return self.q("SELECT * FROM assets WHERE kind=? ORDER BY used_count, id", (kind,))

    def mark_used(self, asset_ids: list[int]) -> None:
        for aid in asset_ids:
            self.x("UPDATE assets SET used_count=used_count+1, last_used_at=? WHERE id=?", (now_iso(), aid))

    # -------------------------------------------------------------- posts
    def create_post(self, **fields: Any) -> int:
        fields.setdefault("created_at", now_iso())
        for k in ("script_json", "asset_ids_json"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.x(f"INSERT INTO posts({cols}) VALUES ({marks})", tuple(fields.values()))

    def update_post(self, post_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))

    def post(self, post_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM posts WHERE id=?", (post_id,))

    def posts(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.q("SELECT * FROM posts WHERE status=? ORDER BY id", (status,))
        return self.q("SELECT * FROM posts ORDER BY id")

    def claim_post(self, post_id: int, from_status: str, to_status: str) -> bool:
        """Атомарно переводит пост из одного статуса в другой; False, если уже не в from_status."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE posts SET status=? WHERE id=? AND status=?", (to_status, post_id, from_status)
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------ metrics
    def add_metrics(self, post_id: int, m: dict[str, int | None]) -> None:
        self.x(
            "INSERT OR REPLACE INTO metrics(post_id, collected_at, plays, reach, likes, comments, saves, "
            "shares, avg_watch_ms, total_watch_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (post_id, now_iso(), m.get("plays"), m.get("reach"), m.get("likes"), m.get("comments"),
             m.get("saves"), m.get("shares"), m.get("avg_watch_ms"), m.get("total_watch_ms")),
        )

    def latest_metrics(self) -> list[sqlite3.Row]:
        """Последний снимок метрик по каждому опубликованному посту."""
        return self.q(
            """SELECT p.id, p.format, p.hook_id, p.theme, p.explore, p.published_at, m.*
               FROM posts p JOIN metrics m ON m.post_id = p.id
               WHERE m.collected_at = (SELECT MAX(collected_at) FROM metrics WHERE post_id = p.id)
               ORDER BY p.id"""
        )

    # ----------------------------------------------------------------- kv
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
