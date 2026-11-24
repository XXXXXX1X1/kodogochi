"""Игровые часы. Все модули берут время отсюда.

Игровое время = epoch + (реальное − epoch) × scale. При scale=1 совпадает с реальным.
В демо (scale=1440) игровые сутки идут за минуту: вместе ускоряются пороги 48 ч,
5 и 7 суток, тихие часы и пятничная граница. epoch хранится в базе, поэтому после
перезапуска игровое время не прыгает назад.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class GameClock:
    def __init__(self, scale: float, epoch: datetime):
        self.scale = scale
        self.epoch = epoch

    def now(self) -> datetime:
        return self.from_real(utcnow())

    def from_real(self, dt: datetime) -> datetime:
        """Перевести реальную метку (например, created_at из GitHub) в игровое время."""
        if self.scale == 1:
            return dt
        return self.epoch + (dt - self.epoch) * self.scale

    def game_seconds(self, real_seconds: float) -> timedelta:
        return timedelta(seconds=real_seconds * self.scale)


class ManualClock(GameClock):
    """Часы для тестов: время двигается вручную."""

    def __init__(self, start: datetime):
        super().__init__(1, start)
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current
