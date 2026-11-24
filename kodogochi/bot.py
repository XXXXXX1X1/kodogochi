"""Telegram-бот: /start, /me, /pet, /disable и подключение репозитория в личном чате.

В группе бот работает в режиме приватности и видит только команды и ответы на свои
сообщения, поэтому имя питомца принимается ответом на вопрос бота (или как /start Бублик).
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import secrets
import sqlite3
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatMemberUpdated, ForceReply, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ReplyKeyboardRemove)

from .clock import iso, parse, utcnow
from .db import load_pet, migrate_chat
from .github import GitHubError, RateLimited
from .rules import view

log = logging.getLogger("kodogochi.bot")
router = Router()

ASK_NAME = "Как назовём питомца?"
SETUP_TTL = timedelta(minutes=30)  # реальное время: настройка не ускоряется в демо
LOGIN_RE = re.compile(r"^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$", re.I)
REPO_RE = re.compile(r"(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
VIEW_TEXT = {"dead": "умер 🪦", "sleeping": "спит 😴", "sick": "болеет 🤒", "sad": "грустит 😔", "ok": "бодр 😊"}
TIMEZONES = [("Новосибирск", "Asia/Novosibirsk"), ("Москва", "Europe/Moscow"),
             ("Екатеринбург", "Asia/Yekaterinburg"), ("Омск", "Asia/Omsk"),
             ("Красноярск", "Asia/Krasnoyarsk"), ("Владивосток", "Asia/Vladivostok")]
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


class Setup(StatesGroup):
    repo = State()
    workflow = State()
    tz = State()
    consent = State()


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def is_admin(message_or_cb, chat_id: int, user_id: int) -> bool:
    member = await message_or_cb.bot.get_chat_member(chat_id, user_id)
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


def pet_card(ctx, chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    team = ctx.db.one("SELECT * FROM team WHERE chat_id = ?", chat_id)
    if team is None or team["status"] == "disabled":
        return ("Питомца здесь пока нет. Администратор группы может завести его командой /start.", None)
    if team["status"] == "pending":
        return (f"{html.escape(team['name'])} ещё не подключён к GitHub. Администратор продолжает "
                "настройку в личном чате с ботом; повторный /start выдаст новую ссылку.", None)
    pet = load_pet(ctx.db.conn, chat_id)
    lines = [f"<b>{html.escape(team['name'])}</b> · {VIEW_TEXT[view(pet)]}",
             f"Здоровье {pet.health}/100 · Настроение {pet.mood}/100"]
    if pet.ci_state == "red" and pet.red_since:
        days = int((ctx.clock.now() - pet.red_since).total_seconds() // 86400)
        lines.append(f"Сборка красная уже {days} сут." if days else "Сборка красная")
    if team["delivery_blocked"]:
        lines.append("⚠️ Бот не смог написать в чат — проверьте его права.")
    url = f"{ctx.settings.public_url}/p/{team['public_slug']}"
    return "\n".join(lines), kb([InlineKeyboardButton(text="Страница", url=url),
                                 InlineKeyboardButton(text="Ссылка на бейдж", callback_data="badge")])


def unblock(ctx, chat_id: int) -> None:
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET delivery_blocked = 0 WHERE chat_id = ? AND delivery_blocked = 1", (chat_id,))


# ---------- группа ----------

@router.message(CommandStart(), F.chat.type.in_(GROUPS))
async def group_start(message: Message, command: CommandObject, ctx):
    unblock(ctx, message.chat.id)
    if not await is_admin(message, message.chat.id, message.from_user.id):
        await message.reply("Подключить питомца может только администратор группы.")
        return
    team = ctx.db.one("SELECT status FROM team WHERE chat_id = ?", message.chat.id)
    if team and team["status"] == "active":
        text, markup = pet_card(ctx, message.chat.id)
        await message.reply("Репозиторий уже подключён.\n\n" + text, reply_markup=markup)
        return
    if command.args:
        await create_pending(message, ctx, command.args.strip())
        return
    await message.reply(ASK_NAME + " Ответьте на это сообщение (1–32 символа).",
                        reply_markup=ForceReply(input_field_placeholder="Например, Бублик"))


@router.message(F.chat.type.in_(GROUPS), F.reply_to_message.text.startswith(ASK_NAME))
async def group_name_reply(message: Message, ctx):
    if message.reply_to_message.from_user.id != message.bot.id or not message.text:
        return
    if not await is_admin(message, message.chat.id, message.from_user.id):
        await message.reply("Назвать питомца может только администратор группы.")
        return
    await create_pending(message, ctx, message.text.strip())


async def create_pending(message: Message, ctx, name: str) -> None:
    if not 1 <= len(name) <= 32 or "\n" in name:
        await message.reply("Имя должно быть от 1 до 32 символов в одну строку. Попробуйте ещё раз: /start Имя")
        return
    token = secrets.token_urlsafe(16)
    now, chat_id = ctx.clock.now(), message.chat.id
    with ctx.db.tx() as c:
        old = c.execute("SELECT status FROM team WHERE chat_id = ?", (chat_id,)).fetchone()
        if old and old["status"] == "disabled":
            c.execute("DELETE FROM team WHERE chat_id = ?", (chat_id,))
            old = None
        if old:
            c.execute("UPDATE team SET name = ?, owner_tg_id = ?, setup_token_hash = ?, setup_expires_at = ?"
                      " WHERE chat_id = ?", (name, message.from_user.id, token_hash(token),
                                             iso(utcnow() + SETUP_TTL), chat_id))
        else:
            c.execute("INSERT INTO team (chat_id, owner_tg_id, name, tz, public_slug, status, created_at,"
                      " setup_token_hash, setup_expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                      (chat_id, message.from_user.id, name, ctx.settings.default_tz, secrets.token_urlsafe(16),
                       iso(now), token_hash(token), iso(utcnow() + SETUP_TTL)))
            c.execute("INSERT INTO pet (chat_id, health, mood, ci_state, updated_at) VALUES (?, 100, 100,"
                      " 'unknown', ?)", (chat_id, iso(now)))
    link = f"https://t.me/{ctx.bot_username}?start={token}"
    await message.reply(
        f"Привет, {html.escape(name)}! 🐣 Осталось подключить репозиторий GitHub.\n"
        "Нажмите кнопку — настройка продолжится в личном чате. Ссылка действует 30 минут.",
        reply_markup=kb([InlineKeyboardButton(text="Продолжить настройку в личном чате →", url=link)]))


@router.message(Command("pet"), F.chat.type.in_(GROUPS))
async def group_pet(message: Message, ctx):
    unblock(ctx, message.chat.id)
    text, markup = pet_card(ctx, message.chat.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "badge")
async def badge_link(cb: CallbackQuery, ctx):
    team = ctx.db.one("SELECT name, public_slug FROM team WHERE chat_id = ? AND status = 'active'",
                      cb.message.chat.id)
    if team is None:
        await cb.answer("Питомец не подключён", show_alert=True)
        return
    url = f"{ctx.settings.public_url}/p/{team['public_slug']}"
    md = f"[![Кодогочи {team['name']}]({url}/badge.svg)]({url})"
    await cb.message.answer("Вставьте строку в README.md репозитория:\n<code>" + html.escape(md) + "</code>")
    await cb.answer()


@router.message(Command("me"), F.chat.type.in_(GROUPS))
async def group_me(message: Message, command: CommandObject, ctx):
    unblock(ctx, message.chat.id)
    chat_id, user = message.chat.id, message.from_user
    if ctx.db.one("SELECT 1 FROM team WHERE chat_id = ? AND status != 'disabled'", chat_id) is None:
        await message.reply("Сначала заведите питомца: /start")
        return
    login = (command.args or "").strip().lstrip("@")
    if not login:
        m = ctx.db.one("SELECT github_login_norm FROM member WHERE chat_id = ? AND tg_user_id = ?", chat_id, user.id)
        await message.reply(f"Ваш логин GitHub: {html.escape(m['github_login_norm'])}" if m
                            else "Напишите свой логин GitHub: /me octocat")
        return
    if not LOGIN_RE.match(login):
        await message.reply("Это не похоже на логин GitHub. Пример: /me octocat")
        return
    alias = (user.first_name or login)[:32]
    try:
        with ctx.db.tx() as c:
            c.execute("INSERT INTO member (chat_id, tg_user_id, github_login_norm, alias) VALUES (?, ?, ?, ?)"
                      " ON CONFLICT (chat_id, tg_user_id) DO UPDATE SET github_login_norm = excluded.github_login_norm,"
                      " alias = excluded.alias", (chat_id, user.id, login.lower(), alias))
    except sqlite3.IntegrityError:  # логин уже привязал другой участник
        await message.reply("Этот логин уже привязал другой участник чата.")
        return
    await message.reply(
        f"Запомнил: {html.escape(login)} — это {html.escape(alias)}. Теперь питомец будет звать вас по имени.\n"
        "Показывать ваше имя на публичной странице в списке «Кто лечил»?",
        reply_markup=kb([InlineKeyboardButton(text="Показать на странице", callback_data=f"optin:{user.id}:1"),
                         InlineKeyboardButton(text="Скрыть", callback_data=f"optin:{user.id}:0")]))


@router.callback_query(F.data.startswith("optin:"))
async def optin(cb: CallbackQuery, ctx):
    _, user_id, value = cb.data.split(":")
    if int(user_id) != cb.from_user.id:
        await cb.answer("Эта кнопка для другого участника", show_alert=True)
        return
    with ctx.db.tx() as c:
        c.execute("UPDATE member SET public_opt_in = ? WHERE chat_id = ? AND tg_user_id = ?",
                  (int(value), cb.message.chat.id, cb.from_user.id))
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Покажу на странице" if value == "1" else "Не буду показывать")


@router.message(Command("disable"), F.chat.type.in_(GROUPS))
async def group_disable(message: Message, ctx):
    if not await is_admin(message, message.chat.id, message.from_user.id):
        await message.reply("Отключить питомца может только администратор группы.")
        return
    await message.reply("Отключить питомца? Сообщения прекратятся, страница и бейдж закроются, "
                        "данные удалятся через 30 дней.",
                        reply_markup=kb([InlineKeyboardButton(text="Отключить", callback_data="disable:yes"),
                                         InlineKeyboardButton(text="Отмена", callback_data="disable:no")]))


@router.callback_query(F.data.startswith("disable:"))
async def disable_confirm(cb: CallbackQuery, ctx):
    if not await is_admin(cb, cb.message.chat.id, cb.from_user.id):
        await cb.answer("Только для администратора", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    if cb.data == "disable:no":
        await cb.answer("Оставляем питомца")
        return
    disable_team(ctx, cb.message.chat.id)
    await cb.message.answer("Питомец отключён. Чтобы завести нового, вызовите /start.")
    await cb.answer()


def disable_team(ctx, chat_id: int) -> None:
    now = ctx.clock.now()
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'disabled', disabled_at = ? WHERE chat_id = ?", (iso(now), chat_id))
        c.execute("UPDATE notification SET status = 'dropped' WHERE chat_id = ? AND status = 'pending'", (chat_id,))


@router.message(F.migrate_to_chat_id)
async def migrated(message: Message, ctx):
    with ctx.db.tx() as c:
        migrate_chat(c, message.chat.id, message.migrate_to_chat_id)
    log.info("chat migrated to supergroup")


@router.my_chat_member()
async def membership(update: ChatMemberUpdated, ctx):
    if update.chat.type not in GROUPS:
        return
    if update.new_chat_member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        if ctx.db.one("SELECT 1 FROM team WHERE chat_id = ? AND status != 'disabled'", update.chat.id):
            disable_team(ctx, update.chat.id)
    else:
        unblock(ctx, update.chat.id)


# ---------- личный чат: настройка ----------

@router.message(CommandStart(deep_link=True), F.chat.type == ChatType.PRIVATE)
async def private_start(message: Message, command: CommandObject, state: FSMContext, ctx):
    team = ctx.db.one("SELECT * FROM team WHERE setup_token_hash = ? AND status = 'pending'",
                      token_hash(command.args))
    if team is None or parse(team["setup_expires_at"]) < utcnow():
        await message.answer("Ссылка устарела или уже использована. Вызовите /start в чате команды ещё раз.",
                             reply_markup=ReplyKeyboardRemove())
        return
    if team["owner_tg_id"] != message.from_user.id:
        await message.answer("Эта ссылка для администратора, который начал подключение.")
        return
    await state.set_state(Setup.repo)
    await state.update_data(chat_id=team["chat_id"], token=command.args)
    await message.answer(f"Настраиваем питомца <b>{html.escape(team['name'])}</b>.\n\n"
                         "Шаг 1. Пришлите репозиторий: <code>owner/repo</code> или ссылку на GitHub.",
                         reply_markup=ReplyKeyboardRemove())


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def private_hello(message: Message):
    await message.answer("Я Кодогочи — питомец команды, который болеет от проблем репозитория GitHub. 🐣\n\n"
                         "1. Добавьте меня в групповой чат команды.\n"
                         "2. Администратор группы отправляет там /start.\n"
                         "3. Я пришлю ссылку, по которой настройка продолжится здесь.",
                         reply_markup=ReplyKeyboardRemove())


@router.message(Setup.repo, F.chat.type == ChatType.PRIVATE)
async def setup_repo(message: Message, state: FSMContext, ctx):
    match = REPO_RE.search((message.text or "").strip())
    if not match:
        await message.answer("Не понял. Пример: <code>octocat/hello-world</code>")
        return
    owner, name = match.groups()
    try:
        repo = await ctx.gh.repo(owner, name)
        workflows = await ctx.gh.workflows(owner, name)
    except RateLimited:
        await message.answer("GitHub временно ограничил запросы. Попробуйте через пару минут.")
        return
    except GitHubError as exc:
        if exc.status == 404:
            await message.answer("Репозиторий не найден. Проверьте название. Для приватного репозитория "
                                 "администратору сервиса нужен токен GitHub с правом чтения.")
            return
        raise
    await state.update_data(repo_id=repo["id"], owner=repo["owner"]["login"], name=repo["name"],
                            private=bool(repo.get("private")), default_branch=repo["default_branch"])
    if not workflows:
        await message.answer("В репозитории нет GitHub Actions. Добавьте файл <code>.github/workflows/ci.yml</code> "
                             "с тестами и пришлите репозиторий ещё раз.")
        return
    if len(workflows) == 1:
        await choose_workflow(message, state, workflows[0])
        return
    await state.set_state(Setup.workflow)
    await state.update_data(workflows={str(w["id"]): [w["name"], w["path"]] for w in workflows[:10]})
    await message.answer("Шаг 2. Какой workflow проверяет код?",
                         reply_markup=kb(*[[InlineKeyboardButton(text=w["name"], callback_data=f"wf:{w['id']}")]
                                           for w in workflows[:10]]))


@router.callback_query(Setup.workflow, F.data.startswith("wf:"))
async def setup_workflow(cb: CallbackQuery, state: FSMContext):
    wf_id = cb.data.removeprefix("wf:")
    data = await state.get_data()
    name, path = data["workflows"][wf_id]
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await choose_workflow(cb.message, state, {"id": int(wf_id), "name": name, "path": path})


async def choose_workflow(message: Message, state: FSMContext, wf: dict) -> None:
    await state.update_data(workflow_id=str(wf["id"]), workflow_path=wf["path"])
    await state.set_state(Setup.tz)
    rows = [[InlineKeyboardButton(text=title, callback_data=f"tz:{zone}")] for title, zone in TIMEZONES]
    await message.answer(f"Сборка: <b>{html.escape(wf['name'])}</b>.\n\n"
                         "Шаг 3. Часовой пояс команды — для тихих часов 23:00–09:00 и пятничного правила. "
                         "Выберите или напишите в формате IANA, например <code>Asia/Tomsk</code>.",
                         reply_markup=kb(*rows))


@router.callback_query(Setup.tz, F.data.startswith("tz:"))
async def setup_tz_button(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await ask_consent(cb.message, state, cb.data.removeprefix("tz:"))


@router.message(Setup.tz, F.chat.type == ChatType.PRIVATE)
async def setup_tz_text(message: Message, state: FSMContext):
    try:
        ZoneInfo((message.text or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        await message.answer("Не знаю такого пояса. Пример: <code>Asia/Novosibirsk</code>")
        return
    await ask_consent(message, state, message.text.strip())


async def ask_consent(message: Message, state: FSMContext, tz: str) -> None:
    await state.update_data(tz=tz)
    await state.set_state(Setup.consent)
    data = await state.get_data()
    hidden = ("\nРепозиторий приватный: ссылки на GitHub, номера PR и логины на странице скрыты."
              if data["private"] else "")
    await message.answer(
        "Шаг 4. У питомца будет публичная страница и бейдж — их видит любой, у кого есть ссылка. "
        "Там показываются имя питомца, здоровье, настроение, журнал событий за 14 дней без имён и "
        "имена участников, которые сами согласились через /me." + hidden + "\n\nПубликуем?",
        reply_markup=kb([InlineKeyboardButton(text="Согласен", callback_data="consent:yes"),
                         InlineKeyboardButton(text="Отмена", callback_data="consent:no")]))


@router.callback_query(Setup.consent, F.data.startswith("consent:"))
async def setup_consent(cb: CallbackQuery, state: FSMContext, ctx):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    data = await state.get_data()
    if cb.data == "consent:no":
        await state.clear()
        await cb.message.answer("Хорошо, подключение отменено. Начать заново — /start в чате команды.")
        return
    hook_id, secret = secrets.token_urlsafe(16), secrets.token_hex(32)
    with ctx.db.tx() as c:
        team = c.execute("SELECT setup_expires_at FROM team WHERE chat_id = ? AND setup_token_hash = ?"
                         " AND status = 'pending'", (data["chat_id"], token_hash(data["token"]))).fetchone()
        if team is None or parse(team["setup_expires_at"]) < utcnow():
            team = None
        else:
            c.execute("DELETE FROM repository WHERE chat_id = ?", (data["chat_id"],))
            c.execute("INSERT INTO repository (chat_id, github_repo_id, owner, name, private, default_branch,"
                      " workflow_id, workflow_path, hook_id, webhook_secret) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (data["chat_id"], data["repo_id"], data["owner"], data["name"], int(data["private"]),
                       data["default_branch"], data["workflow_id"], data["workflow_path"], hook_id, secret))
            c.execute("UPDATE team SET tz = ?, public_consent_at = ?, setup_token_hash = NULL WHERE chat_id = ?",
                      (data["tz"], iso(ctx.clock.now()), data["chat_id"]))
    await state.clear()
    if team is None:
        await cb.message.answer("Ссылка устарела. Вызовите /start в чате команды ещё раз.")
        return
    url = f"{ctx.settings.public_url}/webhooks/github/{hook_id}"
    hooks = f"https://github.com/{data['owner']}/{data['name']}/settings/hooks/new"
    await cb.message.answer(
        "Последний шаг — webhook в GitHub:\n\n"
        f"1. Откройте <b>Settings → Webhooks → Add webhook</b> (кнопка ниже).\n"
        f"2. <b>Payload URL</b>: <code>{url}</code>\n"
        "    <b>Content type</b>: <code>application/json</code>\n"
        f"    <b>Secret</b>: <code>{secret}</code>\n"
        "3. Выберите <b>Let me select individual events</b> и отметьте: <i>Pushes, Workflow runs, "
        "Pull requests, Pull request reviews</i>. Нажмите <b>Add webhook</b>.\n\n"
        "Ожидаем проверку GitHub… Как только придёт сигнал, я напишу в чат команды.\n"
        "Секрет никому не пересылайте.",
        reply_markup=kb([InlineKeyboardButton(text="Открыть настройки webhook", url=hooks)],
                        [InlineKeyboardButton(text="Проверить подключение", callback_data=f"check:{data['chat_id']}")]))


@router.callback_query(F.data.startswith("check:"))
async def check_connection(cb: CallbackQuery, ctx):
    chat_id = int(cb.data.removeprefix("check:"))
    team = ctx.db.one("SELECT status, owner_tg_id FROM team WHERE chat_id = ?", chat_id)
    if team is None or team["owner_tg_id"] != cb.from_user.id:
        await cb.answer("Нет доступа", show_alert=True)
    elif team["status"] == "active":
        await cb.answer("Готово: репозиторий подключён ✅", show_alert=True)
    else:
        await cb.answer("Не удалось проверить webhook. Проверьте адрес и секрет и нажмите Redeliver в GitHub.",
                        show_alert=True)


@router.message(Command("pet"), F.chat.type == ChatType.PRIVATE)
async def private_pet(message: Message):
    await message.answer("Питомец живёт в чате команды — вызовите /pet там.")
