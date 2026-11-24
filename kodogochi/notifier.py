"""Очередь уведомлений: квота 5 в сутки, тихие часы, склейка, приоритет смерти и воскрешения.

Слот квоты резервируется в базе до отправки. Известный отказ Telegram (429, 403, 400)
слот возвращает; неизвестный исход (таймаут, обрыв, перезапуск) — нет, и повтора не будет.
"""
from __future__ import annotations

import html
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat,
                                TelegramNetworkError, TelegramRetryAfter)

from .clock import GameClock, iso, parse
from .db import Database, load_pet, load_pr, migrate_chat
from .rules import Rules
from .voice import Voice

log = logging.getLogger("kodogochi.notifier")
Sender = Callable[[int, str], Awaitable[int]]  # (chat_id, html) -> message_id

THANKS = {"ci_fixed", "revived", "pr_reviewed", "pr_merged"}


def in_quiet(local: datetime, rules: Rules) -> bool:
    t, start, end = local.time(), rules.quiet_start_t, rules.quiet_end_t
    return (t >= start or t < end) if start > end else (start <= t < end)


def next_window(local: datetime, rules: Rules, next_day: bool = False) -> datetime:
    base = local.date() + timedelta(days=1) if next_day else local.date()
    candidate = datetime.combine(base, rules.quiet_end_t, tzinfo=local.tzinfo)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


class Notifier:
    def __init__(self, db: Database, clock: GameClock, rules: Rules, voice: Voice,
                 send: Sender, public_url: str):
        self.db, self.clock, self.rules, self.voice = db, clock, rules, voice
        self.send, self.public_url = send, public_url

    def recover(self) -> None:
        """После перезапуска: отправки, прерванные на середине, считаются неопределёнными."""
        with self.db.tx() as c:
            c.execute("UPDATE notification SET status = 'uncertain' WHERE status = 'sending'")

    async def run_once(self) -> None:
        now = self.clock.now()
        with self.db.tx() as c:
            c.execute("UPDATE notification SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                      (iso(now),))
        chats = self.db.all(
            "SELECT DISTINCT n.chat_id FROM notification n JOIN team t USING (chat_id)"
            " WHERE n.status = 'pending' AND n.not_before <= ? AND t.status = 'active'"
            " AND t.delivery_blocked = 0", iso(now))
        for row in chats:
            await self.deliver_chat(row["chat_id"], now)

    def _stale(self, c, chat_id: int, n) -> bool:
        pet = load_pet(c, chat_id)
        facts = json.loads(n["facts"])
        if n["kind"] == "ci_failed":
            return pet.ci_state != "red"
        if n["kind"] == "ci_fixed":
            return pet.ci_state == "red"
        if n["kind"] == "fell_asleep":
            return not pet.sleeping
        if n["kind"] == "pr_overdue":
            pr = load_pr(c, chat_id, facts["pr_number"])
            return pr is None or pr.credited or pr.state != "open" or pr.draft
        return False

    def _next_batch(self, chat_id: int, now: datetime) -> list:
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT * FROM notification WHERE chat_id = ? AND status = 'pending' AND not_before <= ?"
                " ORDER BY created_at, id", (chat_id, iso(now))).fetchall()
            fresh = []
            for n in rows:
                if self._stale(c, chat_id, n):
                    c.execute("UPDATE notification SET status = 'dropped' WHERE id = ?", (n["id"],))
                else:
                    fresh.append(n)
        priority = [n for n in fresh if n["priority"]]
        return priority[:1] if priority else fresh

    async def deliver_chat(self, chat_id: int, now: datetime) -> None:
        team = self.db.one("SELECT * FROM team WHERE chat_id = ?", chat_id)
        local = now.astimezone(ZoneInfo(team["tz"]))
        while batch := self._next_batch(chat_id, now):
            ids = [n["id"] for n in batch]
            marks = ",".join("?" * len(ids))
            if in_quiet(local, self.rules):
                self._postpone(ids, next_window(local, self.rules))
                return
            local_date = local.date().isoformat()
            batch_id = secrets.token_hex(6)
            with self.db.tx() as c:
                c.execute("INSERT OR IGNORE INTO daily_quota (chat_id, local_date) VALUES (?, ?)",
                          (chat_id, local_date))
                cur = c.execute(
                    "UPDATE daily_quota SET reserved_count = reserved_count + 1"
                    " WHERE chat_id = ? AND local_date = ? AND reserved_count < ?",
                    (chat_id, local_date, self.rules.daily_notifications))
                if cur.rowcount == 1:
                    c.execute(f"UPDATE notification SET status = 'sending', attempts = attempts + 1, batch_id = ?"
                              f" WHERE id IN ({marks})", (batch_id, *ids))
            if cur.rowcount == 0:
                self._postpone(ids, next_window(local, self.rules, next_day=True))
                return
            text = await self._compose(chat_id, team, batch)
            if not await self._send(chat_id, local_date, ids, text, now):
                return

    def _postpone(self, ids: list[int], until_local: datetime) -> None:
        marks = ",".join("?" * len(ids))
        with self.db.tx() as c:
            c.execute(f"UPDATE notification SET not_before = ? WHERE id IN ({marks})", (iso(until_local), *ids))

    def _release(self, c, chat_id: int, local_date: str) -> None:
        c.execute("UPDATE daily_quota SET reserved_count = reserved_count - 1"
                  " WHERE chat_id = ? AND local_date = ? AND reserved_count > 0", (chat_id, local_date))

    async def _send(self, chat_id: int, local_date: str, ids: list[int], text: str, now: datetime) -> bool:
        marks = ",".join("?" * len(ids))
        try:
            message_id = await self.send(chat_id, text)
        except TelegramRetryAfter as exc:
            with self.db.tx() as c:
                self._release(c, chat_id, local_date)
                c.execute(f"UPDATE notification SET status = 'pending', not_before = ? WHERE id IN ({marks})",
                          (iso(now + self.clock.game_seconds(exc.retry_after)), *ids))
            return False
        except TelegramForbiddenError:
            log.warning("chat %s: bot has no access, delivery paused", chat_id)
            with self.db.tx() as c:
                self._release(c, chat_id, local_date)
                c.execute(f"UPDATE notification SET status = 'pending' WHERE id IN ({marks})", ids)
                c.execute("UPDATE team SET delivery_blocked = 1 WHERE chat_id = ?", (chat_id,))
            return False
        except TelegramMigrateToChat as exc:
            with self.db.tx() as c:
                self._release(c, chat_id, local_date)
                c.execute(f"UPDATE notification SET status = 'pending' WHERE id IN ({marks})", ids)
                migrate_chat(c, chat_id, exc.migrate_to_chat_id)
            return False
        except TelegramBadRequest:
            log.exception("chat %s: telegram rejected message", chat_id)
            with self.db.tx() as c:
                self._release(c, chat_id, local_date)
                c.execute(f"UPDATE notification SET status = 'failed' WHERE id IN ({marks})", ids)
            return False
        except (TelegramNetworkError, TimeoutError, OSError):
            log.warning("chat %s: send outcome unknown", chat_id)
            with self.db.tx() as c:
                c.execute(f"UPDATE notification SET status = 'uncertain' WHERE id IN ({marks})", ids)
            return False
        with self.db.tx() as c:
            c.execute(f"UPDATE notification SET status = 'sent', telegram_message_id = ? WHERE id IN ({marks})",
                      (message_id, *ids))
        return True

    # ---------- текст ----------

    def mention(self, chat_id: int, login: str) -> str:
        m = self.db.one("SELECT tg_user_id, alias FROM member WHERE chat_id = ? AND github_login_norm = ?",
                        chat_id, login)
        if m is None:
            return html.escape(login)
        return f'<a href="tg://user?id={m["tg_user_id"]}">{html.escape(m["alias"] or login)}</a>'

    async def _compose(self, chat_id: int, team, batch: list) -> str:
        pet = self.db.one("SELECT health, mood FROM pet WHERE chat_id = ?", chat_id)
        events = []
        for n in batch:
            facts = json.loads(n["facts"])
            events.append({"kind": n["kind"], **{k: facts[k] for k in ("red_days", "pr_wait_hours") if k in facts},
                           "person": facts.get("person")})
        text, _ = await self.voice.compose(team["name"], [{k: v for k, v in e.items() if k != "person"}
                                                          for e in events], pet["health"], pet["mood"])
        lines = [html.escape(text)]
        lines += self._address(chat_id, events)
        if any(e["kind"] == "died" for e in events):
            lines.append(f'<a href="{self.public_url}/p/{team["public_slug"]}">Страница памяти</a>')
        return "\n".join(lines)

    def _address(self, chat_id: int, events: list[dict]) -> list[str]:
        call, thanks, authors = [], [], []
        for e in events:
            person = e["person"]
            if not person:
                continue
            target = thanks if e["kind"] in THANKS else call if e["kind"] == "pr_overdue" else authors
            name = self.mention(chat_id, person)
            if name not in target:
                target.append(name)
        lines = []
        if call:
            lines.append("Зову: " + ", ".join(call) + " и всю команду 🙏")
        if authors:
            lines.append("Кто в курсе: " + ", ".join(authors))
        if thanks:
            lines.append("Спасибо: " + ", ".join(thanks) + " ❤️")
        return lines
