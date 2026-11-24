"""Один asyncio-процесс: FastAPI, long polling aiogram и APScheduler в одном цикле событий."""
from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from . import bot as bot_handlers
from . import engine
from .clock import GameClock, iso, utcnow
from .db import Database
from .github import GitHub, GitHubError, RateLimited
from .notifier import Notifier
from .rules import Rules
from .settings import Settings, load_settings
from .voice import Voice
from .web import router as web_router
from .webhook import router as webhook_router

log = logging.getLogger("kodogochi")
RETENTION = timedelta(days=30)


@dataclass
class Ctx:
    settings: Settings
    rules: Rules
    db: Database
    clock: GameClock
    gh: GitHub
    voice: Voice
    bot: Bot | None = None
    bot_username: str | None = None
    notifier: Notifier | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: set = field(default_factory=set)

    def wake_notifier(self) -> None:
        self._wake.set()

    def on_activated(self, chat_id: int) -> None:
        task = asyncio.get_running_loop().create_task(self.finish_connection(chat_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def finish_connection(self, chat_id: int) -> None:
        """После ping: первичный снимок без отправки истории и сообщение в чат."""
        await sync_team(self, chat_id, initial=True)
        team = self.db.one(
            "SELECT t.name AS pet_name, t.public_slug, t.owner_tg_id, r.owner, r.name AS repo_name, r.private"
            " FROM team t JOIN repository r USING (chat_id) WHERE chat_id = ?", chat_id)
        if self.bot is None or team is None:
            return
        repo = "" if team["private"] else f" {html.escape(team['owner'])}/{html.escape(team['repo_name'])}"
        url = f"{self.settings.public_url}/p/{team['public_slug']}"
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Страница питомца", url=url)]])
        await self.bot.send_message(
            chat_id, f"Готово! Репозиторий{repo} подключён ✅\n<b>{html.escape(team['pet_name'])}</b> теперь живёт "
                     "здесь и следит за сборкой и ревью. Состояние — /pet, привязать свой GitHub — /me логин.",
            reply_markup=markup)
        await self.bot.send_message(team["owner_tg_id"], "Готово: репозиторий подключён ✅ Питомец уже в чате команды.")


async def sync_team(ctx: Ctx, chat_id: int, initial: bool = False) -> None:
    repo = ctx.db.one("SELECT * FROM repository WHERE chat_id = ?", chat_id)
    known_open = [r["number"] for r in ctx.db.all(
        "SELECT number FROM pull_request WHERE chat_id = ? AND state = 'open'", chat_id)]
    try:
        snap = await ctx.gh.snapshot(repo["owner"], repo["name"], repo["workflow_id"], repo["default_branch"],
                                     known_open)
    except (GitHubError, RateLimited, OSError) as exc:
        log.warning("sync failed for chat %s: %s", chat_id, exc)
        with ctx.db.tx() as c:  # таймеры ждут успешной сверки
            c.execute("UPDATE repository SET sync_ok = 0 WHERE chat_id = ?", (chat_id,))
        return
    now = ctx.clock.now()
    with ctx.db.tx() as c:
        engine.apply_snapshot(c, chat_id, snap, ctx.clock, now, ctx.rules, initial)
        engine.tick_team(c, chat_id, now, ctx.rules)
    ctx.wake_notifier()


async def reconcile_all(ctx: Ctx) -> None:
    for row in ctx.db.all("SELECT chat_id FROM team WHERE status = 'active'"):
        await sync_team(ctx, row["chat_id"])


async def tick_all(ctx: Ctx) -> None:
    now = ctx.clock.now()
    rows = ctx.db.all("SELECT t.chat_id FROM team t JOIN repository r USING (chat_id)"
                      " WHERE t.status = 'active' AND r.sync_ok = 1")
    for row in rows:
        with ctx.db.tx() as c:
            engine.tick_team(c, row["chat_id"], now, ctx.rules)
    if rows:
        ctx.wake_notifier()


def purge(ctx: Ctx) -> None:
    """Технические метки и закрытые PR — 30 суток, отключённые команды — удаление через 30 дней."""
    edge = iso(ctx.clock.now() - RETENTION)
    with ctx.db.tx() as c:
        c.execute("DELETE FROM event WHERE occurred_at < ?", (edge,))
        c.execute("DELETE FROM pull_request WHERE state != 'open' AND closed_at < ?", (edge,))
        c.execute("DELETE FROM daily_quota WHERE local_date < ?", (edge[:10],))
        c.execute("DELETE FROM team WHERE status = 'disabled' AND disabled_at < ?", (edge,))
        c.execute("DELETE FROM team WHERE status = 'pending' AND created_at < ?", (edge,))


def backup(ctx: Ctx) -> None:
    """Ежедневная копия SQLite, хранятся 7 последних."""
    folder = ctx.settings.backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"kodogochi-{utcnow():%Y%m%d}.db"
    dest = sqlite3.connect(target)
    with dest:
        ctx.db.conn.backup(dest)
    dest.close()
    for old in sorted(folder.glob("kodogochi-*.db"))[:-7]:
        old.unlink()


async def notifier_loop(ctx: Ctx) -> None:
    while True:
        try:
            await ctx.notifier.run_once()
        except Exception:
            log.exception("notifier pass failed")
        ctx._wake.clear()
        try:
            await asyncio.wait_for(ctx._wake.wait(), ctx.settings.notify_seconds)
        except asyncio.TimeoutError:
            pass


def build_ctx(settings: Settings, bot: Bot | None = None, gh: GitHub | None = None,
              voice: Voice | None = None, clock: GameClock | None = None) -> Ctx:
    db = Database(settings.db_path)
    clock = clock or GameClock(settings.time_scale, db.epoch())
    voice = voice or Voice(settings.config_dir, settings.anthropic_key, settings.llm_model)
    ctx = Ctx(settings=settings, rules=settings.rules(), db=db, clock=clock,
              gh=gh or GitHub(settings.github_token), voice=voice, bot=bot)

    async def send(chat_id: int, text: str) -> int:
        msg = await ctx.bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
        return msg.message_id

    ctx.notifier = Notifier(db, clock, ctx.rules, voice, send, settings.public_url)
    return ctx


def create_app(settings: Settings | None = None, ctx: Ctx | None = None, background: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ctx
        if ctx is None:
            bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) \
                if settings.bot_token else None
            ctx = build_ctx(settings, bot=bot)
        app.state.ctx = ctx
        scheduler, tasks = None, []
        if background:
            ctx.notifier.recover()
            scheduler = AsyncIOScheduler()
            scheduler.add_job(tick_all, "interval", seconds=settings.tick_seconds, args=[ctx],
                              max_instances=1, coalesce=True)
            scheduler.add_job(reconcile_all, "interval", seconds=settings.reconcile_seconds, args=[ctx],
                              max_instances=1, coalesce=True, next_run_time=datetime.now() + timedelta(seconds=10))
            scheduler.add_job(purge, "interval", hours=6, args=[ctx], max_instances=1)
            scheduler.add_job(backup, "interval", hours=24, args=[ctx], max_instances=1)
            scheduler.start()
            tasks.append(asyncio.create_task(notifier_loop(ctx)))
            if ctx.bot is not None:
                me = await ctx.bot.get_me()
                ctx.bot_username = me.username
                dp = Dispatcher(storage=MemoryStorage(), ctx=ctx)
                dp.include_router(bot_handlers.router)
                tasks.append(asyncio.create_task(dp.start_polling(ctx.bot, handle_signals=False)))
            log.info("started: demo=%s scale=%s tick=%ss reconcile=%ss llm=%s", settings.demo,
                     settings.time_scale, settings.tick_seconds, settings.reconcile_seconds,
                     "on" if settings.anthropic_key else "templates")
        yield
        for task in tasks:
            task.cancel()
        if scheduler:
            scheduler.shutdown(wait=False)
        await ctx.gh.close()
        if ctx.bot is not None:
            await ctx.bot.session.close()
        ctx.db.close()

    app = FastAPI(title="Кодогочи", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.include_router(webhook_router)
    app.include_router(web_router)
    return app
