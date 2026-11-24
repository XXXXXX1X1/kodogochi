import asyncio
from datetime import timedelta
from types import SimpleNamespace

import anthropic

from kodogochi import engine
from kodogochi.db import load_pet
from kodogochi.engine import PRInfo, RunInfo, Snapshot
from kodogochi.settings import ROOT
from kodogochi.voice import Voice, acceptable

from .conftest import CHAT, START, ci, make_team, post_event, pr_event, review


# ---------- голос ----------

class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.requests = []

    async def create(self, **kw):
        self.requests.append(kw)
        if self.behaviour == "timeout":
            raise anthropic.APITimeoutError(request=None)
        if self.behaviour == "slow":
            await asyncio.sleep(30)
        text, stop = self.behaviour
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def voice(behaviour):
    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(behaviour)))
    return Voice(ROOT / "config", None, "claude-opus-5-5", client=client), client.beta.messages


def compose(v, events):
    return asyncio.run(v.compose("Бублик", events, 80, 90))


def test_good_reply_used_and_no_names_sent():
    v, m = voice(("Ой, я заболел! Помогите починить сборку 🤒", "end_turn"))
    text, source = compose(v, [{"kind": "ci_failed"}])
    assert source == "llm" and "заболел" in text
    sent = m.requests[0]["messages"][0]["content"]
    assert "person" not in sent and m.requests[0]["model"] == "claude-opus-5-5"


def test_timeout_and_refusal_fall_back_to_template():
    v, _ = voice("timeout")
    text, source = compose(v, [{"kind": "died", "red_days": 5}])
    assert source == "template" and "Здесь покоится Бублик" in text and "5 суток" in text
    v, _ = voice(("нет", "refusal"))
    assert compose(v, [{"kind": "ci_fixed"}])[1] == "template"


def test_invalid_replies_rejected():
    assert not acceptable("Посмотрите https://evil.example", 240)
    assert not acceptable("<b>жирный</b> текст", 240)
    assert not acceptable("а" * 241, 240)
    assert not acceptable("Это bob виноват", 240)
    assert not acceptable("English only reply", 240)
    assert acceptable("Сборка снова зелёная. Спасибо за помощь!", 240)
    v, _ = voice(("@bob почини!", "end_turn"))
    assert compose(v, [{"kind": "ci_failed"}])[1] == "template"


def test_slow_model_times_out(monkeypatch):
    monkeypatch.setattr("kodogochi.voice.TIMEOUT", 0.05)
    v, _ = voice("slow")
    assert compose(v, [{"kind": "fell_asleep"}])[1] == "template"


def test_template_merges_several_events():
    v = Voice(ROOT / "config", None, "m")
    text, source = compose(v, [{"kind": "pr_overdue", "pr_wait_hours": 50}, {"kind": "friday_push"}])
    assert source == "template" and len(text.split("\n")) == 2


# ---------- страница и бейдж ----------

def test_page_shows_state_journal_and_only_opted_in_helpers(client, ctx):
    with ctx.db.tx() as c:
        c.execute("INSERT INTO member VALUES (?, 1, 'bob', 'Боб', 1)", (CHAT,))
        c.execute("INSERT INTO member VALUES (?, 2, 'carol', 'Кэрол', 0)", (CHAT,))
    post_event(client, "workflow_run", ci("failure", 1))
    post_event(client, "workflow_run", ci("success", 2, actor="carol"))
    post_event(client, "pull_request", pr_event("opened"))
    post_event(client, "pull_request_review", review(reviewer="bob"))
    html = client.get("/p/slug1001").text
    assert "Бублик" in html and "Сборка не прошла" in html and "PR #7 получил ревью" in html
    assert "Боб" in html and "Кэрол" not in html
    assert "team/project" in html


def test_private_repo_hides_details(client, ctx):
    make_team(ctx, chat_id=-2002, private=True)
    with ctx.db.tx() as c:
        engine.record(c, -2002, [engine.R.Effect("pr_merged", "k", helper="bob", facts={"pr_number": 3})],
                      ctx.clock.now())
    html = client.get("/p/slug2002").text
    assert "PR принят" in html and "#3" not in html and "github.com/team" not in html and "bob" not in html


def test_journal_window_and_pagination(client, ctx):
    post_event(client, "workflow_run", ci("failure", 1))
    ctx.clock.advance(days=15)
    assert "За последние 14 дней событий пока нет" in client.get("/p/slug1001").text
    assert client.get("/p/slug1001?page=0").status_code == 400
    assert client.get("/p/slug1001?page=abc").status_code == 400
    assert client.get("/p/slug1001?page=99").status_code == 200


def test_badge_svg_and_404s(client, ctx):
    r = client.get("/p/slug1001/badge.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["cache-control"] == "public, max-age=60" and "❤ 100" in r.text
    assert client.get("/p/nope").status_code == 404
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'disabled'")
    assert client.get("/p/slug1001").status_code == 404
    assert client.get("/p/slug1001/badge.svg").status_code == 404


def test_landing_and_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "Тамагочи, который болеет" in client.get("/").text


# ---------- сверка ----------

def test_initial_snapshot_imports_red_streak_without_notifications(ctx):
    runs = [RunInfo(12, 1, "failure", "bob", START - timedelta(days=1)),
            RunInfo(11, 1, "failure", "bob", START - timedelta(days=2)),
            RunInfo(10, 1, "success", "bob", START - timedelta(days=3))]
    prs = [PRInfo(3, "alice", False, START - timedelta(days=1), first_review_at=START, reviewer="bob")]
    with ctx.db.tx() as c:
        engine.apply_snapshot(c, CHAT, Snapshot(runs, prs, START), ctx.clock, ctx.clock.now(), ctx.rules, True)
        engine.tick_team(c, CHAT, ctx.clock.now(), ctx.rules)
    pet = load_pet(ctx.db.conn, CHAT)
    assert pet.ci_state == "red" and pet.red_since == START - timedelta(days=2) and pet.health == 60
    assert ctx.db.one("SELECT credited FROM pull_request WHERE number = 3")["credited"] == 1
    assert ctx.db.one("SELECT COUNT(*) n FROM notification WHERE kind = 'ci_failed'")["n"] == 0


def test_sync_finds_missed_ci_fix_and_closed_pr(client, ctx):
    post_event(client, "workflow_run", ci("failure", 5))
    post_event(client, "pull_request", pr_event("opened", number=9))
    snap = Snapshot([RunInfo(6, 1, "success", "carol", START)],
                    [PRInfo(9, "alice", False, START, state="merged", merger="bob")])
    with ctx.db.tx() as c:
        engine.apply_snapshot(c, CHAT, snap, ctx.clock, ctx.clock.now(), ctx.rules, False)
    assert load_pet(ctx.db.conn, CHAT).ci_state == "green"
    assert ctx.db.one("SELECT state FROM pull_request WHERE number = 9")["state"] == "merged"
    kinds = [r["kind"] for r in ctx.db.all("SELECT kind FROM journal ORDER BY id")]
    assert kinds == ["ci_failed", "ci_fixed", "pr_merged"]


def test_migrate_to_supergroup_keeps_everything(client, ctx):
    post_event(client, "workflow_run", ci("failure", 5))
    with ctx.db.tx() as c:
        engine_db = __import__("kodogochi.db", fromlist=["migrate_chat"])
        engine_db.migrate_chat(c, CHAT, -100999)
    assert ctx.db.one("SELECT COUNT(*) n FROM journal WHERE chat_id = -100999")["n"] == 1
    assert load_pet(ctx.db.conn, -100999).ci_state == "red"
    assert ctx.db.one("SELECT chat_id FROM repository")["chat_id"] == -100999


def test_old_commit_at_connection_does_not_put_pet_to_sleep(ctx):
    with ctx.db.tx() as c:
        c.execute("UPDATE repository SET last_push_at = ?", ((START - timedelta(days=30)).isoformat(),))
        engine.tick_team(c, CHAT, ctx.clock.now(), ctx.rules)
    assert not load_pet(ctx.db.conn, CHAT).sleeping
    ctx.clock.advance(days=7)
    with ctx.db.tx() as c:
        engine.tick_team(c, CHAT, ctx.clock.now(), ctx.rules)
    assert load_pet(ctx.db.conn, CHAT).sleeping
