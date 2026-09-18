"""服务入口。

业务逻辑见：
- service.router  报价路由 / 状态机 / 冻结排队 / 幂等
- service.api     HTTP 适配（/quotes /intents /institutions ...）
- service.store   仅追加事件日志（JOURNAL_PATH，重启重放）
"""

from .api import run

__all__ = ["run"]

if __name__ == "__main__":
    run()
