"""时间抽象：生产用系统时钟，测试用可控假时钟。

全服务统一从这里取时间，保证“决策时钟 / 报价接收时钟 / 报价有效期”
可复现，离线补传与穿越有效期的测试才能确定。
"""

import time


class Clock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)
