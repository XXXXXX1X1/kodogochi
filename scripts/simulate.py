"""Подписанные события «как от GitHub» — для проверки без настоящего репозитория.

    python3 scripts/simulate.py URL SECRET REPO_ID fail
    python3 scripts/simulate.py URL SECRET REPO_ID fix
    python3 scripts/simulate.py URL SECRET REPO_ID pr 12
    python3 scripts/simulate.py URL SECRET REPO_ID review 12
    python3 scripts/simulate.py URL SECRET REPO_ID push
    python3 scripts/simulate.py URL SECRET REPO_ID ping

URL и SECRET — те, что бот выдал при подключении; REPO_ID — числовой id репозитория на GitHub.
WORKFLOW_ID (по умолчанию из переменной окружения) должен совпадать с выбранным при подключении.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone

import httpx


def main() -> None:
    url, secret, repo_id, action = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    number = int(sys.argv[5]) if len(sys.argv) > 5 else 1
    workflow_id = int(os.environ.get("WORKFLOW_ID", "0"))
    branch = os.environ.get("BRANCH", "main")
    actor = {"login": os.environ.get("ACTOR", "octocat")}
    repo = {"id": repo_id, "pushed_at": int(time.time())}
    run_number = int(time.time()) % 1_000_000
    now = datetime.now(timezone.utc).isoformat()

    def run(conclusion: str) -> dict:
        return {"action": "completed", "repository": repo, "workflow_run": {
            "workflow_id": workflow_id, "head_branch": branch, "run_number": run_number, "run_attempt": 1,
            "conclusion": conclusion, "actor": actor, "event": "push"}}

    pr = {"number": number, "user": actor, "draft": False, "created_at": now}
    events = {
        "fail": ("workflow_run", run("failure")),
        "fix": ("workflow_run", run("success")),
        "pr": ("pull_request", {"action": "opened", "repository": repo, "pull_request": pr}),
        "review": ("pull_request_review", {"action": "submitted", "repository": repo,
                                           "pull_request": {**pr, "user": {"login": "pr-author"}},
                                           "review": {"user": actor, "submitted_at": now}}),
        "push": ("push", {"repository": repo, "ref": f"refs/heads/{branch}", "after": secrets.token_hex(20),
                          "deleted": False, "sender": actor}),
        "ping": ("ping", {"zen": "Keep it logically awesome.", "repository": repo}),
    }
    event, payload = events[action]
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = httpx.post(url, content=body, timeout=10, headers={
        "Content-Type": "application/json", "X-GitHub-Event": event,
        "X-GitHub-Delivery": secrets.token_hex(8), "X-Hub-Signature-256": signature})
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
