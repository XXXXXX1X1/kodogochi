"""Движок: применяет правила к базе.

Вызывается из обработчика webhook, часовой сверки и таймеров. Всё внутри одной
транзакции, которую открывает вызывающий код; сетевых вызовов здесь нет.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import rules as R
from .clock import GameClock, iso, parse
from .db import load_open_prs, load_pet, load_pr, save_pet, save_pr

NORMAL_TTL = timedelta(hours=24)
PRIORITY_TTL = timedelta(hours=48)


def label(kind: str, facts: dict, private: bool) -> str:
    pr = "PR" if private or "pr_number" not in facts else f"PR #{facts['pr_number']}"
    return {
        "connected": "Репозиторий подключён",
        "ci_failed": "Сборка не прошла",
        "ci_fixed": "Сборка снова зелёная",
        "died": f"Питомец умер: {facts.get('red_days', 5)} суток красной сборки",
        "revived": "Питомец воскрес: сборку починили",
        "pr_overdue": f"{pr} ждёт ревью больше 2 суток",
        "pr_reviewed": f"{pr} получил ревью",
        "pr_merged": f"{pr} принят",
        "friday_push": "Правка в основную ветку в пятницу вечером",
        "fell_asleep": "Питомец уснул: неделя без правок",
        "woke": "Питомец проснулся",
    }[kind]


def _norm_login(login: str | None) -> str | None:
    return login.lower() if login else None


def record(c: sqlite3.Connection, chat_id: int, effects: list[R.Effect], now: datetime,
           event_id: int | None = None, origin: str = "rule", notify: bool = True) -> int:
    """Журнал и очередь для переходов. Возвращает число новых записей журнала."""
    repo = c.execute("SELECT private FROM repository WHERE chat_id = ?", (chat_id,)).fetchone()
    private = bool(repo and repo["private"])
    pet = load_pet(c, chat_id)
    written = 0
    for e in effects:
        eid = event_id
        if eid is None:
            cur = c.execute(
                "INSERT OR IGNORE INTO event (chat_id, origin, kind, source_key, occurred_at, processed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)", (chat_id, origin, e.kind, e.key, iso(now), iso(now)))
            if cur.rowcount == 0:
                continue  # этот переход уже записан
            eid = cur.lastrowid
        cur = c.execute(
            "INSERT OR IGNORE INTO journal (chat_id, event_id, kind, occurred_at, safe_label, helper_login)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, eid, e.kind, iso(now), label(e.kind, e.facts, private), _norm_login(e.helper)))
        written += cur.rowcount
        if notify and e.notify:
            facts = {**e.facts, "health": pet.health, "mood": pet.mood, "person": _norm_login(e.person)}
            if pet.red_since:
                facts.setdefault("red_days", R.full_days(pet.red_since, now))
            ttl = PRIORITY_TTL if e.priority else NORMAL_TTL
            c.execute(
                "INSERT OR IGNORE INTO notification (chat_id, event_id, kind, dedupe_key, facts, priority,"
                " status, created_at, not_before, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (chat_id, eid, e.kind, e.key, json.dumps(facts, ensure_ascii=False), e.priority,
                 iso(now), iso(now), iso(now + ttl)))
    return written


# ---------- события GitHub ----------

def process_webhook(c: sqlite3.Connection, chat_id: int, event_id: int, ev: dict,
                    now: datetime, rules: R.Rules) -> None:
    team = c.execute("SELECT tz FROM team WHERE chat_id = ?", (chat_id,)).fetchone()
    repo = c.execute("SELECT * FROM repository WHERE chat_id = ?", (chat_id,)).fetchone()
    pet = load_pet(c, chat_id)
    effects: list[R.Effect] = []
    kind = ev["type"]

    if kind == "push":
        if not ev["deleted"]:
            c.execute("UPDATE repository SET last_push_at = ? WHERE chat_id = ?"
                      " AND (last_push_at IS NULL OR last_push_at < ?)", (iso(ev["at"]), chat_id, iso(ev["at"])))
        local = ev["at"].astimezone(ZoneInfo(team["tz"]))
        effects = R.apply_push(pet, ev["branch"], repo["default_branch"], ev["deleted"], ev["after"],
                               ev["pusher"], local, rules)

    elif kind == "ci":
        if workflow_matches(repo, ev) and ev["branch"] == repo["default_branch"]:
            effects = R.apply_ci(pet, ev["conclusion"], ev["run_number"], ev["run_attempt"], ev["actor"], now)
            effects += R.tick_health(pet, now, rules)

    elif kind == "pr":
        pr = load_pr(c, chat_id, ev["number"]) or R.PR(ev["number"], ev["author"], ev["created_at"])
        if ev["action"] == "closed":
            effects = R.apply_pr_closed(pet, pr, ev["merged"], ev["merger"], now, rules)
        else:
            R.pr_open_event(pr, ev["action"], ev["draft"], now)
        save_pr(c, chat_id, pr, now)

    elif kind == "review":
        pr = load_pr(c, chat_id, ev["number"]) or R.PR(ev["number"], ev["author"], ev["created_at"],
                                                       draft=ev["draft"])
        if pr.state == "open" and pr.eligible_since is None and not pr.draft and not pr.credited:
            pr.eligible_since = pr.opened_at
        effects = R.apply_review(pet, pr, ev["reviewer"], now, rules)
        save_pr(c, chat_id, pr, now)

    save_pet(c, chat_id, pet, now)
    record(c, chat_id, effects, now, event_id=event_id)


def workflow_matches(repo: sqlite3.Row, ev: dict) -> bool:
    wanted = str(repo["workflow_id"])
    return wanted == str(ev["workflow_id"]) or (
        repo["workflow_path"] is not None and repo["workflow_path"] == ev.get("path"))


# ---------- сверка с GitHub REST ----------

@dataclass
class RunInfo:
    run_number: int
    run_attempt: int
    conclusion: str
    actor: str | None
    finished_at: datetime  # реальное время


@dataclass
class PRInfo:
    number: int
    author: str
    draft: bool
    created_at: datetime  # реальное время
    state: str = "open"  # open | closed | merged
    first_review_at: datetime | None = None
    reviewer: str | None = None
    merger: str | None = None


@dataclass
class Snapshot:
    runs: list[RunInfo] = field(default_factory=list)  # новые сверху
    prs: list[PRInfo] = field(default_factory=list)
    last_commit_at: datetime | None = None


def _red_streak_start(runs: list[RunInfo]) -> RunInfo | None:
    """Самый ранний красный запуск непрерывной серии, если её начало подтверждено зелёным."""
    significant = [r for r in runs if r.conclusion in R.RED_CONCLUSIONS | R.GREEN_CONCLUSIONS]
    earliest = None
    for run in significant:
        if run.conclusion in R.GREEN_CONCLUSIONS:
            return earliest
        earliest = run
    return None


def apply_snapshot(c: sqlite3.Connection, chat_id: int, snap: Snapshot, clock: GameClock,
                   now: datetime, rules: R.Rules, initial: bool) -> None:
    repo = c.execute("SELECT * FROM repository WHERE chat_id = ?", (chat_id,)).fetchone()
    pet = load_pet(c, chat_id)
    effects: list[R.Effect] = []

    significant = [r for r in snap.runs if r.conclusion in R.RED_CONCLUSIONS | R.GREEN_CONCLUSIONS]
    if significant:
        latest = significant[0]
        red_since = None
        if latest.conclusion in R.RED_CONCLUSIONS:
            start = _red_streak_start(snap.runs)
            if start is not None:
                red_since = clock.from_real(start.finished_at)
            elif not initial:
                red_since = clock.from_real(latest.finished_at)
        effects += R.apply_ci(pet, latest.conclusion, latest.run_number, latest.run_attempt,
                              latest.actor, now, red_since=red_since)

    for info in snap.prs:
        pr = load_pr(c, chat_id, info.number)
        if pr is None:
            if info.state != "open":
                continue
            pr = R.PR(info.number, info.author.lower(), clock.from_real(info.created_at))
            R.pr_open_event(pr, "opened", info.draft, now)
        elif pr.state == "open" and info.draft != pr.draft:
            R.pr_open_event(pr, "converted_to_draft" if info.draft else "ready_for_review", info.draft, now)
        if info.first_review_at and not pr.credited and info.reviewer and info.reviewer.lower() != pr.author:
            if initial:
                pr.credited, pr.first_review_at = True, clock.from_real(info.first_review_at)
            else:
                effects += R.apply_review(pet, pr, info.reviewer.lower(), now, rules)
        if info.state != "open" and pr.state == "open":
            effects += R.apply_pr_closed(pet, pr, info.state == "merged",
                                         info.merger.lower() if info.merger else None, now, rules)
        save_pr(c, chat_id, pr, now)

    if snap.last_commit_at:
        commit_at = clock.from_real(snap.last_commit_at)
        known = parse(repo["last_push_at"])
        if known is None or commit_at > known:
            c.execute("UPDATE repository SET last_push_at = ? WHERE chat_id = ?", (iso(commit_at), chat_id))
            if pet.sleeping:
                pet.sleeping = False
                effects.append(R.Effect("woke", f"woke:{iso(commit_at)}", notify=False))

    save_pet(c, chat_id, pet, now)
    record(c, chat_id, effects, now, origin="sync", notify=not initial)
    c.execute("UPDATE repository SET sync_ok = 1, last_sync_at = ? WHERE chat_id = ?", (iso(now), chat_id))


def tick_team(c: sqlite3.Connection, chat_id: int, now: datetime, rules: R.Rules, notify: bool = True) -> None:
    """Суточные ступени: здоровье, штрафы за PR, восстановление настроения, спячка."""
    repo = c.execute("SELECT last_push_at, connected_at FROM repository WHERE chat_id = ?",
                     (chat_id,)).fetchone()
    pet = load_pet(c, chat_id)
    prs = load_open_prs(c, chat_id)
    # Спячка считается не раньше подключения: старые правки — история, а не повод уснуть сразу.
    marks = [t for t in (parse(repo["last_push_at"]), parse(repo["connected_at"])) if t is not None]
    last_push = max(marks) if marks else now
    effects = R.tick(pet, prs, last_push, now, rules)
    for pr in prs:
        save_pr(c, chat_id, pr, now)
    save_pet(c, chat_id, pet, now)
    record(c, chat_id, effects, now, notify=notify)


def activate(c: sqlite3.Connection, chat_id: int, now: datetime) -> bool:
    """Первый подписанный ping: команда становится активной. False, если уже была активна."""
    cur = c.execute("UPDATE team SET status = 'active' WHERE chat_id = ? AND status = 'pending'"
                    " AND public_consent_at IS NOT NULL", (chat_id,))
    if cur.rowcount == 0:
        return False
    c.execute("UPDATE repository SET connected_at = ? WHERE chat_id = ?", (iso(now), chat_id))
    record(c, chat_id, [R.Effect("connected", f"connected:{iso(now)}", notify=False)], now, origin="sync")
    return True
