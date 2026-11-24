import json

from kodogochi.db import load_pet

from .conftest import CHAT, HOOK, ci, post_event, pr_event, push, review


def notifications(ctx):
    return [r["kind"] for r in ctx.db.all("SELECT kind FROM notification WHERE chat_id = ? ORDER BY id", CHAT)]


def test_signed_push_accepted_and_duplicate_ignored(client, ctx):
    r = post_event(client, "push", push(), delivery="same")
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    r = post_event(client, "push", push(), delivery="same")
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    assert ctx.db.one("SELECT COUNT(*) n FROM event WHERE origin = 'webhook'")["n"] == 1


def test_bad_signature_401(client):
    r = post_event(client, "push", push(), secret="wrong")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "bad_signature" and r.json()["error"]["request_id"].startswith("req-")


def test_push_without_repository_id_422(client):
    payload = push()
    del payload["repository"]["id"]
    assert post_event(client, "push", payload).status_code == 422


def test_other_repository_409(client):
    assert post_event(client, "push", push(repo_id=99)).status_code == 409


def test_invalid_json_400_and_missing_headers_400(client):
    assert post_event(client, "push", None, raw=b"{not json").status_code == 400
    r = client.post(f"/webhooks/github/{HOOK}", content=b"{}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_headers"


def test_too_large_413(client):
    assert post_event(client, "push", None, raw=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413


def test_unknown_hook_404(client):
    assert post_event(client, "push", push(), hook="nope").status_code == 404


def test_unsupported_event_ignored(client):
    r = post_event(client, "issues", {"action": "opened", "repository": {"id": 42}})
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    r = post_event(client, "workflow_run", {**ci("failure", 1), "action": "requested"})
    assert r.json() == {"status": "ignored"}


def test_bad_sha_422(client):
    assert post_event(client, "push", push(after="zzz")).status_code == 422


def test_ci_flow_through_webhook(client, ctx):
    assert post_event(client, "workflow_run", ci("failure", 5, actor="Bob")).status_code == 202
    pet = load_pet(ctx.db.conn, CHAT)
    assert pet.ci_state == "red" and pet.red_actor == "bob"
    facts = json.loads(ctx.db.one("SELECT facts FROM notification WHERE kind = 'ci_failed'")["facts"])
    assert facts["person"] == "bob"
    # другая ветка и другой workflow не трогают состояние
    post_event(client, "workflow_run", ci("success", 6, branch="feature"))
    post_event(client, "workflow_run", ci("success", 7, workflow_id=1))
    assert load_pet(ctx.db.conn, CHAT).ci_state == "red"
    post_event(client, "workflow_run", ci("success", 8, actor="carol"))
    assert load_pet(ctx.db.conn, CHAT).ci_state == "green"
    assert notifications(ctx) == ["ci_failed", "ci_fixed"]
    labels = [r["safe_label"] for r in ctx.db.all("SELECT safe_label FROM journal ORDER BY id")]
    assert labels == ["Сборка не прошла", "Сборка снова зелёная"]


def test_pr_and_review_flow(client, ctx):
    post_event(client, "pull_request", pr_event("opened"))
    row = ctx.db.one("SELECT * FROM pull_request WHERE number = 7")
    assert row["state"] == "open" and row["eligible_since"] is not None
    post_event(client, "pull_request_review", review())
    assert ctx.db.one("SELECT credited FROM pull_request WHERE number = 7")["credited"] == 1
    assert ctx.db.one("SELECT helper_login FROM journal WHERE kind = 'pr_reviewed'")["helper_login"] == "bob"
    assert notifications(ctx) == []  # свежий PR: ревью без сообщения в чат


def test_friday_push_via_webhook_uses_team_timezone(client, ctx):
    # пятница 27.11, 18:30 по Новосибирску = 11:30 UTC
    post_event(client, "push", push(after="f" * 40, pushed_at=1795779000))
    assert notifications(ctx) == ["friday_push"]


def test_pending_team_ignores_events(client, ctx):
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'pending'")
    assert post_event(client, "push", push()).json() == {"status": "ignored"}


def test_ping_activates_pending_team(client, ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(ctx, "on_activated", calls.append)
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'pending'")
    assert post_event(client, "ping", {"zen": "hi", "repository": {"id": 42}}).status_code == 202
    assert ctx.db.one("SELECT status FROM team")["status"] == "active" and calls == [CHAT]
    post_event(client, "ping", {"zen": "hi", "repository": {"id": 42}})
    assert calls == [CHAT]  # повторный ping не активирует заново


def test_disabled_team_404(client, ctx):
    with ctx.db.tx() as c:
        c.execute("UPDATE team SET status = 'disabled'")
    assert post_event(client, "push", push()).status_code == 404
