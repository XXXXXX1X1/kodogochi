"""POST /webhooks/github/{hook_id}: подпись, лимит размера, дубли, нормализация."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import engine
from .clock import UTC, GameClock, iso, parse

log = logging.getLogger("kodogochi.webhook")
router = APIRouter()

MAX_BODY = 2 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_ACTIONS = {"opened", "reopened", "closed", "ready_for_review", "converted_to_draft"}


class InvalidPayload(ValueError):
    pass


def error(status: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "request_id": request_id}},
                        status_code=status)


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _login(obj: Any) -> str | None:
    if isinstance(obj, dict) and isinstance(obj.get("login"), str):
        return obj["login"].lower()[:39]
    return None


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPayload("ожидалось целое число")
    return value


def normalize(event: str, p: dict, clock: GameClock, now) -> dict | None:
    """Полезные поля события. None — тип или действие не поддерживаются."""
    try:
        if event == "push":
            ref = p["ref"]
            if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
                return None  # теги не считаются правками
            after = p["after"]
            if not isinstance(after, str) or not SHA_RE.match(after):
                raise InvalidPayload("after должен быть SHA")
            pushed = p.get("repository", {}).get("pushed_at")
            at = clock.from_real(parse_ts(pushed)) if pushed else now
            return {"type": "push", "branch": ref.removeprefix("refs/heads/"),
                    "deleted": bool(p.get("deleted", False)), "after": after,
                    "pusher": _login(p.get("sender")), "at": at}
        if event == "workflow_run":
            if p.get("action") != "completed":
                return None
            run = p["workflow_run"]
            return {"type": "ci", "workflow_id": _int(run["workflow_id"]), "path": run.get("path"),
                    "branch": run.get("head_branch"), "run_number": _int(run["run_number"]),
                    "run_attempt": _int(run.get("run_attempt", 1)),
                    "conclusion": str(run.get("conclusion") or ""), "actor": _login(run.get("actor"))}
        if event == "pull_request":
            if p.get("action") not in PR_ACTIONS:
                return None
            pr = p["pull_request"]
            return {"type": "pr", "action": p["action"], "number": _int(pr["number"]),
                    "author": _login(pr["user"]) or "unknown", "draft": bool(pr.get("draft", False)),
                    "created_at": clock.from_real(parse_ts(pr["created_at"])),
                    "merged": bool(pr.get("merged", False)), "merger": _login(pr.get("merged_by"))}
        if event == "pull_request_review":
            if p.get("action") != "submitted":
                return None
            pr, review = p["pull_request"], p["review"]
            reviewer = _login(review["user"])
            if not reviewer:
                raise InvalidPayload("нет автора ревью")
            return {"type": "review", "number": _int(pr["number"]), "reviewer": reviewer,
                    "author": _login(pr["user"]) or "unknown", "draft": bool(pr.get("draft", False)),
                    "created_at": clock.from_real(parse_ts(pr["created_at"]))}
    except (KeyError, TypeError, AttributeError) as exc:
        raise InvalidPayload(str(exc)) from exc
    return None


def parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        return parse(value)
    raise InvalidPayload("неверная дата")


def minimal(ev: dict) -> str:
    return json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in ev.items()},
                      ensure_ascii=False)


@router.post("/webhooks/github/{hook_id}")
async def github_webhook(hook_id: str, request: Request):
    ctx = request.app.state.ctx
    rid = "req-" + secrets.token_hex(4)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY:
        return error(413, "payload_too_large", "Событие больше 2 МиБ", rid)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return error(413, "payload_too_large", "Событие больше 2 МиБ", rid)

    repo = ctx.db.one("SELECT r.*, t.status FROM repository r JOIN team t USING (chat_id)"
                      " WHERE r.hook_id = ?", hook_id)
    if repo is None or repo["status"] == "disabled":
        return error(404, "not_found", "Адрес не найден", rid)

    event = request.headers.get("x-github-event")
    delivery = request.headers.get("x-github-delivery")
    signature = request.headers.get("x-hub-signature-256")
    if not (event and delivery and signature):
        return error(400, "missing_headers", "Нужны X-GitHub-Event, X-GitHub-Delivery и X-Hub-Signature-256", rid)
    if not signature_ok(repo["webhook_secret"], bytes(body), signature):
        log.warning("bad signature hook=%s %s", hook_id[:6], rid)
        return error(401, "bad_signature", "Подпись не сходится", rid)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error(400, "invalid_json", "Некорректный JSON", rid)
    if not isinstance(payload, dict):
        return error(400, "invalid_json", "Ожидался JSON-объект", rid)

    repo_id = payload.get("repository", {}).get("id") if isinstance(payload.get("repository"), dict) else None
    if not isinstance(repo_id, int):
        return error(422, "invalid_payload", "Нет repository.id", rid)
    if repo_id != repo["github_repo_id"]:
        log.warning("repo mismatch hook=%s %s", hook_id[:6], rid)
        return error(409, "repository_mismatch", "Событие другого репозитория", rid)

    chat_id = repo["chat_id"]
    now = ctx.clock.now()
    if event == "ping":
        with ctx.db.tx() as c:
            activated = engine.activate(c, chat_id, now)
        if activated:
            ctx.on_activated(chat_id)
        return JSONResponse({"status": "accepted"}, status_code=202)
    if repo["status"] != "active":
        return JSONResponse({"status": "ignored"}, status_code=200)

    try:
        ev = normalize(event, payload, ctx.clock, now)
    except InvalidPayload as exc:
        return error(422, "invalid_payload", f"Проверьте формат события: {exc}", rid)
    if ev is None:
        return JSONResponse({"status": "ignored"}, status_code=200)

    try:
        with ctx.db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO event (chat_id, origin, kind, source_key, occurred_at, processed_at,"
                " payload_min) VALUES (?, 'webhook', ?, ?, ?, ?, ?)",
                (chat_id, ev["type"], delivery[:64], iso(now), iso(now), minimal(ev)))
            if cur.rowcount == 0:
                return JSONResponse({"status": "duplicate"}, status_code=200)
            engine.process_webhook(c, chat_id, cur.lastrowid, ev, now, ctx.rules)
    except sqlite3.Error:
        log.exception("sqlite error %s", rid)
        return error(503, "unavailable", "Питомец временно недоступен", rid)
    ctx.wake_notifier()
    return JSONResponse({"status": "accepted"}, status_code=202)
