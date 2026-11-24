"""Шесть правил питомца. Чистые функции: без сети, базы и системных часов.

Каждая функция меняет переданные Pet/PR и возвращает список Effect —
переходов, которые движок запишет в журнал и очередь уведомлений.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

DAY = timedelta(days=1)
RED_CONCLUSIONS = {"failure", "timed_out"}
GREEN_CONCLUSIONS = {"success"}
PRIORITY_KINDS = {"died", "revived"}


@dataclass(frozen=True)
class Rules:
    health_loss_per_day: int = 20
    mood_step: int = 10
    mood_recovery_per_day: int = 10
    review_wait_hours: int = 48
    sleep_after_days: int = 7
    daily_notifications: int = 5
    quiet_start: str = "23:00"
    quiet_end: str = "09:00"
    friday_after: str = "18:00"

    @property
    def review_wait(self) -> timedelta:
        return timedelta(hours=self.review_wait_hours)

    @property
    def quiet_start_t(self) -> time:
        return time.fromisoformat(self.quiet_start)

    @property
    def quiet_end_t(self) -> time:
        return time.fromisoformat(self.quiet_end)

    @property
    def friday_after_t(self) -> time:
        return time.fromisoformat(self.friday_after)


@dataclass
class Pet:
    health: int = 100
    mood: int = 100
    ci_state: str = "unknown"  # unknown | green | red
    run_number: int | None = None
    run_attempt: int | None = None
    red_since: datetime | None = None
    red_actor: str | None = None
    sleeping: bool = False
    dead: bool = False
    mood_anchor_at: datetime | None = None


@dataclass
class PR:
    number: int
    author: str
    opened_at: datetime
    state: str = "open"  # open | closed | merged
    draft: bool = False
    eligible_since: datetime | None = None
    first_review_at: datetime | None = None
    last_penalty_day: int = 0
    credited: bool = False
    closed_at: datetime | None = None


@dataclass
class Effect:
    kind: str
    key: str
    helper: str | None = None  # кто «лечил»: попадает в список на странице
    person: str | None = None  # к кому обратиться в чате
    facts: dict = field(default_factory=dict)
    notify: bool = True

    @property
    def priority(self) -> int:
        return 1 if self.kind in PRIORITY_KINDS else 0


def clamp(value: int) -> int:
    return max(0, min(100, value))


def view(pet: Pet) -> str:
    """Вид питомца: умер → спит → болеет → грустит → бодр."""
    if pet.dead:
        return "dead"
    if pet.sleeping:
        return "sleeping"
    if pet.ci_state == "red":
        return "sick"
    if pet.mood < 60:
        return "sad"
    return "ok"


def full_days(start: datetime, now: datetime) -> int:
    return max(0, int((now - start) // DAY))


# ---------- 1–2. Сборка упала / починена ----------

def apply_ci(pet: Pet, conclusion: str, run_number: int, run_attempt: int,
             actor: str | None, now: datetime, red_since: datetime | None = None) -> list[Effect]:
    """Завершённый запуск CI основной ветки. Старые запуски состояние не откатывают."""
    if conclusion not in RED_CONCLUSIONS | GREEN_CONCLUSIONS:
        return []  # отменённые и прочие исходы оставляют последний значимый результат
    version = (run_number, run_attempt)
    if pet.run_number is not None and version <= (pet.run_number, pet.run_attempt or 0):
        return []
    pet.run_number, pet.run_attempt = version
    key = f"ci:{run_number}:{run_attempt}"
    if conclusion in RED_CONCLUSIONS:
        if pet.ci_state == "red":
            return []
        pet.ci_state = "red"
        pet.red_since = red_since or now
        pet.red_actor = actor
        return [Effect("ci_failed", key, person=actor)]
    was_red, was_dead = pet.ci_state == "red", pet.dead
    pet.ci_state = "green"
    pet.red_since = None
    pet.red_actor = None
    pet.health = 100
    pet.dead = False
    if not was_red:
        return []
    return [Effect("revived" if was_dead else "ci_fixed", key, helper=actor, person=actor)]


def tick_health(pet: Pet, now: datetime, rules: Rules) -> list[Effect]:
    if pet.ci_state != "red" or pet.red_since is None:
        return []
    days = full_days(pet.red_since, now)
    pet.health = clamp(100 - rules.health_loss_per_day * days)
    if pet.health == 0 and not pet.dead:
        pet.dead = True
        return [Effect("died", f"died:{pet.red_since.isoformat()}",
                       person=pet.red_actor, facts={"red_days": days})]
    return []


# ---------- 3–4. PR ждёт ревью / PR проверили ----------

def pr_waiting(pr: PR) -> bool:
    return pr.state == "open" and not pr.draft and not pr.credited and pr.eligible_since is not None


def tick_prs(pet: Pet, prs: list[PR], now: datetime, rules: Rules) -> list[Effect]:
    effects: list[Effect] = []
    overdue = False
    for pr in prs:
        if not pr_waiting(pr):
            continue
        wait = now - pr.eligible_since
        if wait <= rules.review_wait:  # ровно 48 часов — ещё не штраф
            continue
        overdue = True
        steps = 1 + int((wait - rules.review_wait) // DAY)
        if steps <= pr.last_penalty_day:
            continue
        pet.mood = clamp(pet.mood - rules.mood_step * (steps - pr.last_penalty_day))
        if pr.last_penalty_day == 0:
            effects.append(Effect(
                "pr_overdue", f"pr_overdue:{pr.number}:{pr.eligible_since.isoformat()}",
                person=pr.author,
                facts={"pr_number": pr.number, "pr_wait_hours": int(wait.total_seconds() // 3600)}))
        pr.last_penalty_day = steps
    if overdue:
        pet.mood_anchor_at = None
    elif pet.mood_anchor_at is None or pet.mood >= 100:
        pet.mood_anchor_at = now
    else:
        days = full_days(pet.mood_anchor_at, now)
        if days:
            pet.mood = clamp(pet.mood + rules.mood_recovery_per_day * days)
            pet.mood_anchor_at += days * DAY
    return effects


def pr_open_event(pr: PR, action: str, draft: bool, now: datetime) -> None:
    """opened / reopened / ready_for_review / converted_to_draft."""
    pr.draft = draft
    if action == "converted_to_draft":
        pr.eligible_since = None
        return
    if action == "opened":
        pr.state = "open"
        pr.eligible_since = None if draft else pr.opened_at
        return
    if action == "reopened":
        pr.state, pr.closed_at = "open", None
    if not pr.credited and not pr.draft:
        pr.eligible_since = now
        pr.last_penalty_day = 0


def _credit(pet: Pet, pr: PR, kind: str, helper: str | None, now: datetime,
            rules: Rules) -> list[Effect]:
    if pr.credited:
        return []
    was_overdue = pr.last_penalty_day > 0
    pr.credited = True
    pr.first_review_at = pr.first_review_at or now
    pet.mood = clamp(pet.mood + rules.mood_step)
    return [Effect(kind, f"credit:{pr.number}", helper=helper, person=helper,
                   facts={"pr_number": pr.number}, notify=was_overdue)]


def apply_review(pet: Pet, pr: PR, reviewer: str, now: datetime, rules: Rules) -> list[Effect]:
    if reviewer == pr.author:
        return []  # ревью собственного PR не лечит
    return _credit(pet, pr, "pr_reviewed", reviewer, now, rules)


def apply_pr_closed(pet: Pet, pr: PR, merged: bool, merger: str | None,
                    now: datetime, rules: Rules) -> list[Effect]:
    pr.state = "merged" if merged else "closed"
    pr.closed_at = now
    if not merged:
        return []  # закрытие без merge снимает PR с проверки без бонуса
    return _credit(pet, pr, "pr_merged", merger, now, rules)


# ---------- 5–6. Пятничная правка / спячка ----------

def apply_push(pet: Pet, branch: str, default_branch: str, deleted: bool, after: str,
               pusher: str | None, local_time: datetime, rules: Rules) -> list[Effect]:
    """local_time — время push в часовом поясе команды."""
    if deleted:
        return []
    effects: list[Effect] = []
    if pet.sleeping:
        pet.sleeping = False
        effects.append(Effect("woke", f"woke:{after}", notify=False))
    if (branch == default_branch and local_time.weekday() == 4
            and local_time.time() > rules.friday_after_t):
        effects.append(Effect("friday_push", f"friday:{branch}:{after}", person=pusher))
    return effects


def tick_sleep(pet: Pet, last_push_at: datetime, now: datetime, rules: Rules) -> list[Effect]:
    if pet.sleeping or now - last_push_at < timedelta(days=rules.sleep_after_days):
        return []
    pet.sleeping = True
    return [Effect("fell_asleep", f"sleep:{last_push_at.isoformat()}")]


def tick(pet: Pet, prs: list[PR], last_push_at: datetime, now: datetime, rules: Rules) -> list[Effect]:
    return tick_health(pet, now, rules) + tick_prs(pet, prs, now, rules) + tick_sleep(pet, last_push_at, now, rules)
