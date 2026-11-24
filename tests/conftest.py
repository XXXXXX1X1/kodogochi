import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from kodogochi.app import build_ctx, create_app
from kodogochi.clock import ManualClock, iso
from kodogochi.settings import load_settings

CHAT = -1001
SECRET = "s3cret"
HOOK = "hook123"
REPO_ID = 42
# понедельник, 10:00 по Новосибирску (UTC+7)
START = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)


class FakeVoice:
    def __init__(self):
        self.calls = []

    async def compose(self, pet_name, events, health, mood):
        self.calls.append(events)
        return " + ".join(e["kind"] for e in events), "template"


class FakeSender:
    def __init__(self):
        self.sent = []
        self.error = None

    async def __call__(self, chat_id, text):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return len(self.sent)


@pytest.fixture
def ctx(tmp_path):
    settings = load_settings({"DB_PATH": str(tmp_path / "t.db"), "BACKUP_DIR": str(tmp_path / "b"),
                              "PUBLIC_URL": "https://pet.test"})
    clock = ManualClock(START)
    c = build_ctx(settings, clock=clock, voice=FakeVoice())
    c.sender = FakeSender()
    c.notifier.send = c.sender
    make_team(c)
    yield c
    c.db.close()


def make_team(ctx, chat_id=CHAT, private=False, status="active"):
    now = iso(ctx.clock.now())
    with ctx.db.tx() as c:
        c.execute("INSERT INTO team (chat_id, owner_tg_id, name, tz, public_slug, status, created_at,"
                  " public_consent_at) VALUES (?, 7, 'Бублик', 'Asia/Novosibirsk', ?, ?, ?, ?)",
                  (chat_id, f"slug{abs(chat_id)}", status, now, now))
        c.execute("INSERT INTO pet (chat_id, health, mood, ci_state, updated_at) VALUES (?, 100, 100, 'green', ?)",
                  (chat_id, now))
        c.execute("INSERT INTO repository (chat_id, github_repo_id, owner, name, private, default_branch,"
                  " workflow_id, workflow_path, hook_id, webhook_secret, connected_at, sync_ok, last_push_at)"
                  " VALUES (?, ?, 'team', 'project', ?, 'main', '555', '.github/workflows/ci.yml', ?, ?, ?, 1, ?)",
                  (chat_id, REPO_ID, int(private), HOOK if chat_id == CHAT else f"hook{abs(chat_id)}",
                   SECRET, now, now))


@pytest.fixture
def client(ctx):
    app = create_app(ctx.settings, ctx=ctx, background=False)
    with TestClient(app) as tc:
        yield tc


def sign(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


_delivery = [0]


def post_event(client, event, payload, delivery=None, secret=SECRET, hook=HOOK, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    _delivery[0] += 1
    return client.post(f"/webhooks/github/{hook}", content=body, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery or f"d-{_delivery[0]}",
        "X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"})


def push(after="a" * 40, branch="main", deleted=False, sender="octocat", repo_id=REPO_ID, pushed_at=None):
    p = {"repository": {"id": repo_id}, "ref": f"refs/heads/{branch}", "after": after, "deleted": deleted,
         "sender": {"login": sender}}
    if pushed_at is not None:
        p["repository"]["pushed_at"] = pushed_at
    return p


def ci(conclusion, run_number, attempt=1, actor="octocat", branch="main", workflow_id=555):
    return {"action": "completed", "repository": {"id": REPO_ID},
            "workflow_run": {"workflow_id": workflow_id,
                             "path": ".github/workflows/ci.yml" if workflow_id == 555 else ".github/workflows/lint.yml", "head_branch": branch,
                             "run_number": run_number, "run_attempt": attempt, "conclusion": conclusion,
                             "actor": {"login": actor}, "event": "push"}}


def pr_event(action, number=7, author="alice", draft=False, merged=False, merger=None, created_at=None):
    return {"action": action, "repository": {"id": REPO_ID},
            "pull_request": {"number": number, "user": {"login": author}, "draft": draft, "merged": merged,
                             "merged_by": {"login": merger} if merger else None,
                             "created_at": created_at or "2026-11-23T03:00:00Z"}}


def review(number=7, reviewer="bob", author="alice"):
    return {"action": "submitted", "repository": {"id": REPO_ID},
            "pull_request": {"number": number, "user": {"login": author}, "created_at": "2026-11-23T03:00:00Z"},
            "review": {"user": {"login": reviewer}, "state": "approved", "submitted_at": "2026-11-23T05:00:00Z"}}
