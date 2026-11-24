import asyncio
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from kodogochi import engine
from kodogochi import rules as R
from kodogochi.clock import parse

from .conftest import CHAT


def add(ctx, kind, key, **facts):
    with ctx.db.tx() as c:
        engine.record(c, CHAT, [R.Effect(kind, key, facts=facts, person=facts.pop("person", None))], ctx.clock.now())


def run(ctx):
    asyncio.run(ctx.notifier.run_once())


def statuses(ctx):
    return [r["status"] for r in ctx.db.all("SELECT status FROM notification ORDER BY id")]


def quota(ctx):
    row = ctx.db.one("SELECT reserved_count FROM daily_quota")
    return row["reserved_count"] if row else 0


def test_pending_events_merge_into_one_message(ctx):
    add(ctx, "friday_push", "f1", person="bob")
    add(ctx, "fell_asleep", "s1")
    with ctx.db.tx() as c:
        c.execute("UPDATE pet SET sleeping = 1")
    run(ctx)
    assert len(ctx.sender.sent) == 1 and statuses(ctx) == ["sent", "sent"]
    text = ctx.sender.sent[0][1]
    assert "friday_push + fell_asleep" in text and "Кто в курсе: bob" in text
    assert quota(ctx) == 1


def test_sixth_message_waits_for_next_day(ctx):
    for i in range(6):
        add(ctx, "friday_push", f"f{i}")
        run(ctx)
    assert len(ctx.sender.sent) == 5 and statuses(ctx)[-1] == "pending"
    nb = parse(ctx.db.one("SELECT not_before FROM notification WHERE status = 'pending'")["not_before"])
    assert nb.astimezone(parse("2026-01-01T00:00:00+07:00").tzinfo).hour == 9
    assert nb > ctx.clock.now() + timedelta(hours=12)


def test_quiet_hours_postpone_until_nine(ctx):
    ctx.clock.advance(hours=13)  # 23:00 по Новосибирску
    add(ctx, "friday_push", "late")
    run(ctx)
    assert ctx.sender.sent == []
    nb = parse(ctx.db.one("SELECT not_before FROM notification")["not_before"])
    assert nb == ctx.clock.now() + timedelta(hours=10)
    ctx.clock.advance(hours=10)
    run(ctx)
    assert len(ctx.sender.sent) == 1


def test_stale_ci_failed_is_dropped(ctx):
    add(ctx, "ci_failed", "ci:1:1")  # сборка в фикстуре зелёная
    run(ctx)
    assert ctx.sender.sent == [] and statuses(ctx) == ["dropped"]


def test_death_and_revival_delivered_separately_in_order(ctx):
    add(ctx, "died", "d1", red_days=5)
    add(ctx, "revived", "r1")
    run(ctx)
    assert [t.split("\n")[0] for _, t in ctx.sender.sent] == ["died", "revived"]
    assert "Страница памяти" in ctx.sender.sent[0][1]


def test_retry_after_releases_slot(ctx):
    ctx.sender.error = TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "Too Many Requests", 30)
    add(ctx, "friday_push", "f")
    run(ctx)
    assert statuses(ctx) == ["pending"] and quota(ctx) == 0
    nb = parse(ctx.db.one("SELECT not_before FROM notification")["not_before"])
    assert nb == ctx.clock.now() + timedelta(seconds=30)


def test_forbidden_blocks_delivery(ctx):
    ctx.sender.error = TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "bot was kicked")
    add(ctx, "friday_push", "f")
    run(ctx)
    assert ctx.db.one("SELECT delivery_blocked FROM team")["delivery_blocked"] == 1 and quota(ctx) == 0


def test_unknown_outcome_is_uncertain_and_keeps_slot(ctx):
    ctx.sender.error = TelegramNetworkError(SendMessage(chat_id=1, text="x"), "timeout")
    add(ctx, "friday_push", "f")
    run(ctx)
    ctx.sender.error = None
    run(ctx)
    assert statuses(ctx) == ["uncertain"] and quota(ctx) == 1 and ctx.sender.sent == []


def test_restart_marks_sending_uncertain(ctx):
    add(ctx, "friday_push", "f")
    with ctx.db.tx() as c:
        c.execute("UPDATE notification SET status = 'sending'")
    ctx.notifier.recover()
    assert statuses(ctx) == ["uncertain"]


def test_mention_uses_linked_member(ctx):
    with ctx.db.tx() as c:
        c.execute("INSERT INTO member (chat_id, tg_user_id, github_login_norm, alias) VALUES (?, 555, 'bob', 'Боб')",
                  (CHAT,))
    add(ctx, "friday_push", "f", person="bob")
    run(ctx)
    assert '<a href="tg://user?id=555">Боб</a>' in ctx.sender.sent[0][1]


def test_expired_notification_not_sent(ctx):
    add(ctx, "friday_push", "f")
    ctx.clock.advance(hours=25)
    run(ctx)
    assert statuses(ctx) == ["expired"]


@pytest.mark.parametrize("hours", [0])
def test_disabled_team_gets_nothing(ctx, hours):
    add(ctx, "friday_push", "f")
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'disabled'")
    run(ctx)
    assert ctx.sender.sent == []
