"""Настройки из переменных окружения и config/rules.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .rules import Rules

ROOT = Path(__file__).resolve().parent.parent


def _read_secret_file(path: str) -> str | None:
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    return text or None


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    public_url: str
    db_path: Path
    backup_dir: Path
    default_tz: str
    time_scale: float  # 1 — обычный режим, 1440 — демо: игровые сутки за 1 минуту
    anthropic_key: str | None
    llm_model: str
    github_token: str | None
    tick_seconds: float
    reconcile_seconds: float
    notify_seconds: float
    config_dir: Path

    @property
    def demo(self) -> bool:
        return self.time_scale > 1

    def rules(self) -> Rules:
        data = tomllib.loads((self.config_dir / "rules.toml").read_text(encoding="utf-8"))
        return Rules(**data["rules"])


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    scale = float(env.get("TIME_SCALE", "1"))
    demo = scale > 1
    github_token = _read_secret_file(env.get("GITHUB_TOKEN_FILE", ""))
    # Без токена GitHub даёт 60 запросов в час на IP — в демо сверяем реже.
    default_reconcile = "3600" if not demo else ("60" if github_token else "300")
    return Settings(
        bot_token=env.get("BOT_TOKEN") or None,
        public_url=env.get("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        db_path=Path(env.get("DB_PATH", str(ROOT / "data" / "kodogochi.db"))),
        backup_dir=Path(env.get("BACKUP_DIR", str(ROOT / "data" / "backups"))),
        default_tz=env.get("DEFAULT_TZ", "Asia/Novosibirsk"),
        time_scale=scale,
        anthropic_key=_read_secret_file(env.get("ANTHROPIC_KEY_FILE", "")),
        llm_model=env.get("LLM_MODEL", "claude-opus-5-5"),
        github_token=github_token,
        tick_seconds=float(env.get("TICK_SECONDS", "5" if demo else "3600")),
        reconcile_seconds=float(env.get("RECONCILE_SECONDS", default_reconcile)),
        notify_seconds=float(env.get("NOTIFY_SECONDS", "3" if demo else "20")),
        config_dir=Path(env.get("CONFIG_DIR", str(ROOT / "config"))),
    )
