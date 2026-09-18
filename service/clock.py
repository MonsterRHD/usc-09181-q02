"""时钟抽象。

路由决策必须记录时钟信息（墙钟、单调钟、逻辑序号），
测试使用 ManualClock 保证跨有效期边界等场景的确定性。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(ts: datetime) -> datetime:
    """朴素时间一律按 UTC 解释，避免时钟信息含糊。"""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class Clock:
    """时钟接口：wall 用于有效期/新鲜度判定，mono 仅作决策记录。"""

    def wall(self) -> datetime:  # pragma: no cover - 接口定义
        raise NotImplementedError

    def mono(self) -> float:  # pragma: no cover - 接口定义
        raise NotImplementedError


class SystemClock(Clock):
    def wall(self) -> datetime:
        return utcnow()

    def mono(self) -> float:
        return time.monotonic()


class ManualClock(Clock):
    """测试用手动时钟：时间只在显式推进时变化。"""

    def __init__(self, start: datetime):
        self._now = ensure_utc(start)
        self._mono = 0.0

    def wall(self) -> datetime:
        return self._now

    def mono(self) -> float:
        return self._mono

    def set(self, ts: datetime) -> None:
        self._now = ensure_utc(ts)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
