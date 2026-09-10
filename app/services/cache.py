"""Маленький асинхронный TTL-кэш.

Нужен, чтобы не долбить сайты-источники одинаковыми запросами: поиск, списки
эпизодов и плееров меняются редко, а вот прямые ссылки на видео живут недолго,
поэтому для них TTL задаётся отдельно и держится маленьким.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """Кэш ключ -> значение с общим TTL и защитой от «стада» (dogpile).

    Параллельные вызовы :meth:`get_or_set` с одним ключом ждут один и тот же
    расчёт, а не запускают его несколько раз.
    """

    def __init__(self, ttl: float, *, max_size: int = 512) -> None:
        self.ttl = ttl
        self.max_size = max_size
        self._data: dict[Any, _Entry[T]] = {}
        self._locks: dict[Any, asyncio.Lock] = {}

    def get(self, key: Any) -> T | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return entry.value

    def set(self, key: Any, value: T, *, ttl: float | None = None) -> T:
        if len(self._data) >= self.max_size:
            self._evict()
        self._data[key] = _Entry(value=value, expires_at=time.monotonic() + (ttl or self.ttl))
        return value

    def invalidate(self, key: Any) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    async def get_or_set(
        self, key: Any, factory: Callable[[], Awaitable[T]], *, ttl: float | None = None
    ) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self.get(key)  # мог посчитать тот, кто держал лок до нас
            if cached is not None:
                return cached
            try:
                value = await factory()
            finally:
                self._locks.pop(key, None)
            return self.set(key, value, ttl=ttl)

    def _evict(self) -> None:
        """Чистим протухшее, а если всё живое — самое близкое к протуханию."""
        now = time.monotonic()
        expired = [key for key, entry in self._data.items() if entry.expires_at < now]
        for key in expired:
            self._data.pop(key, None)
        if len(self._data) < self.max_size:
            return
        oldest = sorted(self._data.items(), key=lambda item: item[1].expires_at)
        for key, _ in oldest[: max(1, self.max_size // 4)]:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)
