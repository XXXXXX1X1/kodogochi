"""GitHub REST: подключение репозитория и часовая сверка.

Ответы кешируются по ETag: ответ 304 не тратит квоту GitHub.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .clock import parse
from .engine import PRInfo, RunInfo, Snapshot

log = logging.getLogger("kodogochi.github")
API = "https://api.github.com"
PASS_BUDGET = 30  # запросов на репозиторий за один проход сверки


class GitHubError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"GitHub {status} {message}")
        self.status = status


class RateLimited(GitHubError):
    def __init__(self, retry_after: float):
        super().__init__(429, f"retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class BudgetExhausted(Exception):
    pass


class GitHub:
    def __init__(self, token: str | None = None, base_url: str = API,
                 transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "kodogochi"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10, transport=transport)
        self._etags: dict[str, tuple[str, object]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def get(self, path: str, params: dict | None = None, budget: list[int] | None = None):
        if budget is not None:
            if budget[0] <= 0:
                raise BudgetExhausted(path)
            budget[0] -= 1
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        headers = {}
        if key in self._etags:
            headers["If-None-Match"] = self._etags[key][0]
        resp = await self.http.get(path, params=params, headers=headers)
        if resp.status_code == 304:
            return self._etags[key][1]
        if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"):
            retry = resp.headers.get("retry-after")
            if retry is None and resp.headers.get("x-ratelimit-reset"):
                retry = float(resp.headers["x-ratelimit-reset"]) - datetime.now().timestamp()
            raise RateLimited(max(1.0, float(retry or 60)))
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, resp.json().get("message", "") if resp.content else "")
        data = resp.json()
        if etag := resp.headers.get("etag"):
            self._etags[key] = (etag, data)
        return data

    # ---------- подключение ----------

    async def repo(self, owner: str, name: str) -> dict:
        return await self.get(f"/repos/{owner}/{name}")

    async def workflows(self, owner: str, name: str) -> list[dict]:
        data = await self.get(f"/repos/{owner}/{name}/actions/workflows", {"per_page": 30})
        return [w for w in data.get("workflows", []) if w.get("state") == "active"]

    # ---------- сверка ----------

    async def snapshot(self, owner: str, name: str, workflow_id: str, branch: str,
                       known_open: list[int]) -> Snapshot:
        budget = [PASS_BUDGET]
        base = f"/repos/{owner}/{name}"
        runs = await self.get(f"{base}/actions/workflows/{workflow_id}/runs",
                              {"branch": branch, "status": "completed", "per_page": 10}, budget)
        snap = Snapshot(runs=[
            RunInfo(r["run_number"], r.get("run_attempt", 1), r.get("conclusion") or "",
                    (r.get("actor") or {}).get("login"), parse(r["updated_at"]))
            for r in runs.get("workflow_runs", []) if r.get("event") != "pull_request"])

        pulls = await self.get(f"{base}/pulls", {"state": "open", "per_page": 100}, budget)
        open_numbers = set()
        for p in pulls:
            open_numbers.add(p["number"])
            snap.prs.append(PRInfo(p["number"], p["user"]["login"], bool(p.get("draft")),
                                   parse(p["created_at"])))
        try:
            for info in snap.prs:
                reviews = await self.get(f"{base}/pulls/{info.number}/reviews", {"per_page": 30}, budget)
                for rv in reviews:
                    login = (rv.get("user") or {}).get("login")
                    if rv.get("submitted_at") and login and login.lower() != info.author.lower():
                        info.first_review_at, info.reviewer = parse(rv["submitted_at"]), login
                        break
            for number in known_open:
                if number in open_numbers:
                    continue
                p = await self.get(f"{base}/pulls/{number}", budget=budget)
                snap.prs.append(PRInfo(number, p["user"]["login"], bool(p.get("draft")), parse(p["created_at"]),
                                       state="merged" if p.get("merged") else "closed",
                                       merger=(p.get("merged_by") or {}).get("login")))
        except BudgetExhausted:
            log.info("budget exhausted for %s/%s, continue next pass", owner, name)

        commits = await self.get(f"{base}/commits", {"sha": branch, "per_page": 1})
        if commits:
            snap.last_commit_at = parse(commits[0]["commit"]["committer"]["date"])
        return snap
