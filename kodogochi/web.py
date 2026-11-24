"""Страница питомца, бейдж и проверка запуска. Открытые маршруты только читают."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .art import pet_svg
from .art import badge_svg as render_badge
from .clock import iso, parse
from .db import load_pet
from .rules import view as pet_view

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

PAGE_SIZE = 25
WINDOW = timedelta(days=14)
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
VIEW_TITLE = {"ok": "бодр", "sad": "грустит", "sick": "болеет", "sleeping": "спит", "dead": "умер"}
HELPER_ROLE = {"ci_fixed": "починил сборку", "revived": "воскресил питомца",
               "pr_reviewed": "ревью", "pr_merged": "принял PR"}
KIND_ICON = {"ci_failed": "🤒", "ci_fixed": "💊", "died": "🪦", "revived": "✨", "pr_overdue": "😔",
             "pr_reviewed": "👀", "pr_merged": "🎉", "friday_push": "😱", "fell_asleep": "😴",
             "woke": "☀️", "connected": "🔌"}


def fmt_time(value: str, tz: str) -> str:
    dt = parse(value).astimezone(ZoneInfo(tz))
    return f"{dt.day} {MONTHS[dt.month - 1]}, {dt:%H:%M}"


def page_error(request: Request, status: int, message: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "error.html", {"message": message}, status_code=status)


def active_team(ctx, slug: str):
    return ctx.db.one("SELECT * FROM team WHERE public_slug = ? AND status = 'active'", slug)


@router.get("/healthz")
async def healthz(request: Request):
    request.app.state.ctx.db.one("SELECT 1")
    return JSONResponse({"status": "ok"})


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    ctx = request.app.state.ctx
    return templates.TemplateResponse(request, "landing.html", {
        "bot_username": ctx.bot_username, "pets": {v: Markup(pet_svg(v, 96)) for v in VIEW_TITLE},
        "titles": VIEW_TITLE, "demo": ctx.settings.demo})


@router.get("/p/{slug}", response_class=HTMLResponse)
async def pet_page(request: Request, slug: str, page: str = "1"):
    ctx = request.app.state.ctx
    if not page.isdigit() or int(page) < 1 or int(page) > 10_000:
        return page_error(request, 400, "Неверный номер страницы журнала.")
    team = active_team(ctx, slug)
    if team is None:
        return page_error(request, 404, "Страница питомца недоступна.")
    chat_id, number = team["chat_id"], int(page)
    pet = load_pet(ctx.db.conn, chat_id)
    repo = ctx.db.one("SELECT * FROM repository WHERE chat_id = ?", chat_id)
    since = iso(ctx.clock.now() - WINDOW)
    rows = ctx.db.all("SELECT kind, occurred_at, safe_label FROM journal WHERE chat_id = ? AND occurred_at >= ?"
                      " ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
                      chat_id, since, PAGE_SIZE + 1, (number - 1) * PAGE_SIZE)
    helpers = ctx.db.all(
        "SELECT m.alias, j.kind, COUNT(*) AS times FROM journal j JOIN member m"
        " ON m.chat_id = j.chat_id AND m.github_login_norm = j.helper_login"
        " WHERE j.chat_id = ? AND j.occurred_at >= ? AND m.public_opt_in = 1"
        " GROUP BY m.alias, j.kind ORDER BY MAX(j.occurred_at) DESC", chat_id, since)
    view = pet_view(pet)
    url = f"{ctx.settings.public_url}/p/{slug}"
    return templates.TemplateResponse(request, "pet.html", {
        "name": team["name"], "view": view, "title": VIEW_TITLE[view], "pet": pet,
        "art": Markup(pet_svg(view, 200)),
        "events": [{"time": fmt_time(r["occurred_at"], team["tz"]), "label": r["safe_label"],
                    "icon": KIND_ICON.get(r["kind"], "•")} for r in rows[:PAGE_SIZE]],
        "helpers": [{"alias": h["alias"], "role": HELPER_ROLE.get(h["kind"], h["kind"]), "times": h["times"]}
                    for h in helpers],
        "page": number, "has_next": len(rows) > PAGE_SIZE,
        "repo_url": None if repo["private"] else f"https://github.com/{repo['owner']}/{repo['name']}",
        "repo_name": None if repo["private"] else f"{repo['owner']}/{repo['name']}",
        "badge_md": f"[![Кодогочи {team['name']}]({url}/badge.svg)]({url})",
        "bot_username": ctx.bot_username, "demo": ctx.settings.demo})


@router.get("/p/{slug}/badge.svg")
async def badge(request: Request, slug: str):
    ctx = request.app.state.ctx
    team = active_team(ctx, slug)
    if team is None:
        return Response("Страница питомца недоступна", status_code=404, media_type="text/plain; charset=utf-8")
    pet = load_pet(ctx.db.conn, team["chat_id"])
    svg = render_badge(team["name"], pet_view(pet), pet.health)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=60"})
