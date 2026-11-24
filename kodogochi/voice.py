"""Голос питомца: реплика от Claude, при сбое — русский шаблон.

В модель уходит только обезличенный JSON фактов: тип события, показатели, число часов
и суток. Логины, тексты PR и код не передаются; обращение добавляет сервер.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import tomllib
from collections import defaultdict
from pathlib import Path

import anthropic

log = logging.getLogger("kodogochi.voice")

TIMEOUT = 10.0
REPLY_LIMIT = 240
OBITUARY_LIMIT = 320
MODEL_FACTS = ("red_days", "pr_wait_hours")
BAD_MARKUP = re.compile(r"[<>*_`\[\]#|{}\\]|https?://|www\.|t\.me|@", re.I)
STOP_WORDS = ("виноват", "дурак", "идиот", "тупой", "тупая", "бездар", "позор", "лузер")
CYRILLIC = re.compile(r"[а-яё]", re.I)


def limit_for(kinds: list[str]) -> int:
    return OBITUARY_LIMIT if "died" in kinds else REPLY_LIMIT


def acceptable(text: str, limit: int) -> bool:
    text = text.strip()
    lower = text.lower()
    return (bool(text) and len(text) <= limit and not BAD_MARKUP.search(text)
            and not any(w in lower for w in STOP_WORDS) and CYRILLIC.search(text) is not None
            and text.count("\n") <= 2)


class Voice:
    def __init__(self, config_dir: Path, api_key: str | None, model: str, client=None):
        self.templates: dict[str, list[str]] = tomllib.loads(
            (config_dir / "templates.toml").read_text(encoding="utf-8"))["templates"]
        self.system = (config_dir / "prompts" / "system.txt").read_text(encoding="utf-8")
        self.model = model
        self.client = client or (anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT, max_retries=0)
                                 if api_key else None)
        self._lock = asyncio.Lock()  # один запрос к модели одновременно

    def template(self, pet_name: str, events: list[dict]) -> str:
        lines = []
        for ev in events:
            values = defaultdict(lambda: "?", {"name": pet_name, **ev})
            lines.append(random.choice(self.templates[ev["kind"]]).format_map(values))
        return "\n".join(lines)

    async def compose(self, pet_name: str, events: list[dict], health: int, mood: int) -> tuple[str, str]:
        """events: [{"kind": ..., "red_days": ..., "pr_wait_hours": ...}]. Возвращает (текст, источник)."""
        kinds = [e["kind"] for e in events]
        if self.client is not None:
            facts = {"pet_name": pet_name, "health": health, "mood": mood,
                     "events": [{"kind": e["kind"], **{k: e[k] for k in MODEL_FACTS if k in e}} for e in events]}
            try:
                async with self._lock:
                    text = await asyncio.wait_for(self._ask(facts), TIMEOUT)
                if text is not None and acceptable(text, limit_for(kinds)):
                    return text.strip(), "llm"
                log.info("model reply rejected, using template")
            except (anthropic.APIError, asyncio.TimeoutError) as exc:
                log.warning("model unavailable: %s", type(exc).__name__)
        return self.template(pet_name, events), "template"

    async def _ask(self, facts: dict) -> str | None:
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self.system,
            messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason != "end_turn":
            return None  # refusal или обрыв по длине — берём шаблон
        return "".join(b.text for b in response.content if b.type == "text")
