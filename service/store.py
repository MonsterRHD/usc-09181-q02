"""仅追加事件日志。

所有状态变更先落盘（flush + fsync）再经同一个 ``_apply`` 进入内存，
恢复时重放同一函数，保证“已锁定的价格”等状态在服务重启后不丢失、
不出现两条分叉的状态路径。
"""

import json
import os


class Journal:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def append(self, event_type: str, at: float, payload: dict) -> None:
        line = json.dumps(
            {"type": event_type, "at": at, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def replay(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def close(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
