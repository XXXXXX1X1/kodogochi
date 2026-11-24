"""SQLite: схема раздела 3 ТЗ и чтение/запись состояния питомца.

В базу пишет один asyncio-процесс. Транзакции короткие и без сетевых вызовов внутри.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .clock import iso, parse, utcnow
from .rules import PR, Pet

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS team (
    chat_id INTEGER PRIMARY KEY,
    owner_tg_id INTEGER NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 32),
    tz TEXT NOT NULL,
    public_slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
    created_at TEXT NOT NULL,
    public_consent_at TEXT,
    setup_token_hash TEXT UNIQUE,
    setup_expires_at TEXT,
    delivery_blocked INTEGER NOT NULL DEFAULT 0,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS repository (
    chat_id INTEGER PRIMARY KEY REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    github_repo_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    default_branch TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_path TEXT,
    hook_id TEXT NOT NULL UNIQUE,
    webhook_secret TEXT NOT NULL,
    connected_at TEXT,
    last_push_at TEXT,
    last_sync_at TEXT,
    sync_ok INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pet (
    chat_id INTEGER PRIMARY KEY REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    health INTEGER NOT NULL CHECK (health BETWEEN 0 AND 100),
    mood INTEGER NOT NULL CHECK (mood BETWEEN 0 AND 100),
    ci_state TEXT NOT NULL CHECK (ci_state IN ('unknown', 'green', 'red')),
    run_number INTEGER,
    run_attempt INTEGER,
    red_since TEXT,
    red_actor TEXT,
    sleeping INTEGER NOT NULL DEFAULT 0,
    dead INTEGER NOT NULL DEFAULT 0,
    mood_anchor_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member (
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    tg_user_id INTEGER NOT NULL,
    github_login_norm TEXT NOT NULL,
    alias TEXT,
    public_opt_in INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, tg_user_id),
    UNIQUE (chat_id, github_login_norm),
    CHECK (public_opt_in = 0 OR alias IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS pull_request (
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    number INTEGER NOT NULL,
    author_login TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'merged')),
    draft INTEGER NOT NULL DEFAULT 0,
    first_review_at TEXT,
    eligible_since TEXT,
    last_penalty_day INTEGER NOT NULL DEFAULT 0,
    credited INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    PRIMARY KEY (chat_id, number)
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    origin TEXT NOT NULL CHECK (origin IN ('webhook', 'rule', 'sync')),
    kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    processed_at TEXT,
    payload_min TEXT,
    UNIQUE (chat_id, origin, source_key)
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    event_id INTEGER NOT NULL REFERENCES event (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    safe_label TEXT NOT NULL,
    helper_login TEXT,
    UNIQUE (event_id, kind)
);
CREATE INDEX IF NOT EXISTS journal_by_time ON journal (chat_id, occurred_at);

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    event_id INTEGER NOT NULL REFERENCES event (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    facts TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'sending', 'sent', 'uncertain', 'dropped', 'expired', 'failed')),
    created_at TEXT NOT NULL,
    not_before TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT,
    telegram_message_id INTEGER,
    UNIQUE (chat_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS daily_quota (
    chat_id INTEGER NOT NULL REFERENCES team (chat_id) ON DELETE CASCADE ON UPDATE CASCADE,
    local_date TEXT NOT NULL,
    reserved_count INTEGER NOT NULL DEFAULT 0 CHECK (reserved_count BETWEEN 0 AND 5),
    PRIMARY KEY (chat_id, local_date)
);
"""


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def epoch(self) -> datetime:
        """Начало отсчёта игровых часов: первый запуск этой базы."""
        row = self.one("SELECT value FROM meta WHERE key = 'epoch'")
        if row:
            return parse(row["value"])
        now = utcnow()
        self.conn.execute("INSERT INTO meta (key, value) VALUES ('epoch', ?)", (iso(now),))
        return now

    def close(self) -> None:
        self.conn.close()


# ---------- питомец и PR ----------

def load_pet(c: sqlite3.Connection, chat_id: int) -> Pet:
    r = c.execute("SELECT * FROM pet WHERE chat_id = ?", (chat_id,)).fetchone()
    return Pet(health=r["health"], mood=r["mood"], ci_state=r["ci_state"],
               run_number=r["run_number"], run_attempt=r["run_attempt"],
               red_since=parse(r["red_since"]), red_actor=r["red_actor"],
               sleeping=bool(r["sleeping"]), dead=bool(r["dead"]),
               mood_anchor_at=parse(r["mood_anchor_at"]))


def save_pet(c: sqlite3.Connection, chat_id: int, pet: Pet, now: datetime) -> None:
    c.execute(
        """UPDATE pet SET health=?, mood=?, ci_state=?, run_number=?, run_attempt=?, red_since=?,
           red_actor=?, sleeping=?, dead=?, mood_anchor_at=?, updated_at=? WHERE chat_id=?""",
        (pet.health, pet.mood, pet.ci_state, pet.run_number, pet.run_attempt, iso(pet.red_since),
         pet.red_actor, int(pet.sleeping), int(pet.dead), iso(pet.mood_anchor_at), iso(now), chat_id))


def _row_to_pr(r: sqlite3.Row) -> PR:
    return PR(number=r["number"], author=r["author_login"], opened_at=parse(r["opened_at"]),
              state=r["state"], draft=bool(r["draft"]), eligible_since=parse(r["eligible_since"]),
              first_review_at=parse(r["first_review_at"]), last_penalty_day=r["last_penalty_day"],
              credited=bool(r["credited"]), closed_at=parse(r["closed_at"]))


def load_pr(c: sqlite3.Connection, chat_id: int, number: int) -> PR | None:
    r = c.execute("SELECT * FROM pull_request WHERE chat_id = ? AND number = ?",
                  (chat_id, number)).fetchone()
    return _row_to_pr(r) if r else None


def load_open_prs(c: sqlite3.Connection, chat_id: int) -> list[PR]:
    rows = c.execute("SELECT * FROM pull_request WHERE chat_id = ? AND state = 'open' ORDER BY number",
                     (chat_id,)).fetchall()
    return [_row_to_pr(r) for r in rows]


def save_pr(c: sqlite3.Connection, chat_id: int, pr: PR, now: datetime) -> None:
    c.execute(
        """INSERT INTO pull_request (chat_id, number, author_login, opened_at, updated_at, state, draft,
               first_review_at, eligible_since, last_penalty_day, credited, closed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (chat_id, number) DO UPDATE SET author_login=excluded.author_login,
               updated_at=excluded.updated_at, state=excluded.state, draft=excluded.draft,
               first_review_at=excluded.first_review_at, eligible_since=excluded.eligible_since,
               last_penalty_day=excluded.last_penalty_day, credited=excluded.credited,
               closed_at=excluded.closed_at""",
        (chat_id, pr.number, pr.author, iso(pr.opened_at), iso(now), pr.state, int(pr.draft),
         iso(pr.first_review_at), iso(pr.eligible_since), pr.last_penalty_day, int(pr.credited),
         iso(pr.closed_at)))


def migrate_chat(c: sqlite3.Connection, old_id: int, new_id: int) -> None:
    """Группа стала супергруппой: ON UPDATE CASCADE переносит все дочерние записи."""
    c.execute("UPDATE team SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
