"""HTTP 接口层（仅标准库）。

路由：
  GET  /health                          健康检查
  POST /quotes/snapshot                 机构报价快照（整体替换，支持离线补传，snapshot_id 幂等）
  POST /quotes/withdraw                 撤回报价
  POST /intents                         提交购汇意图（idempotency_key 幂等）
  GET  /intents/{id}                    查询意图状态与分配
  POST /intents/{id}/cancel             撤销意图
  POST /intents/{id}/evaluate           手动触发重评估
  POST /fills                           登记（部分）成交（fill_id 幂等）
  POST /allocations/{id}/release        释放分配未成交余量
  GET  /allocations/{id}                查询分配
  POST /institutions/{name}/freeze      人工冻结机构
  POST /institutions/{name}/unfreeze    解冻并补处理未决意图
  POST /institutions/{name}/limit       设置机构额度
  POST /blacklist                       合规黑名单增删 {institution, blacklisted}
  GET  /decisions/{id}                  取回决策记录（候选快照/理由/时钟）
  GET  /state                           全量状态（审计调试用）

启动：PORT 环境变量指定端口（默认 8000），DB_PATH 指定 SQLite 路径
（默认 ./router.db）。所有变更在引擎锁内串行执行并写穿落库，
进程重启后已锁定的价格不丢失。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import EngineError, QuoteRouter


def make_handler(router: QuoteRouter):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---- 基础工具 ----

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise EngineError("BAD_JSON", "请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise EngineError("BAD_JSON", "请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]
            try:
                if method == "GET" and parts == ["health"]:
                    return self._send(200, {"status": "ok"})
                if method == "GET" and parts == ["state"]:
                    return self._send(200, router.state_dump())
                if method == "GET" and len(parts) == 2 and parts[0] == "intents":
                    return self._send(200, router.get_intent(parts[1]))
                if method == "GET" and len(parts) == 2 and parts[0] == "decisions":
                    return self._send(200, router.get_decision(parts[1]))
                if method == "GET" and len(parts) == 2 and parts[0] == "allocations":
                    return self._send(200, router.get_allocation(parts[1]))

                if method == "POST":
                    if parts == ["quotes", "snapshot"]:
                        b = self._body()
                        return self._send(200, router.ingest_snapshot(
                            b["institution"], b.get("quotes", []),
                            snapshot_id=b.get("snapshot_id")))
                    if parts == ["quotes", "withdraw"]:
                        b = self._body()
                        return self._send(200, router.withdraw_quote(
                            b["institution"], b["quote_id"]))
                    if parts == ["intents"]:
                        b = self._body()
                        return self._send(200, router.submit_intent(
                            b["idempotency_key"], b["ccy_pair"], b["amount"],
                            intent_id=b.get("intent_id")))
                    if parts == ["fills"]:
                        b = self._body()
                        return self._send(200, router.record_fill(
                            b["allocation_id"], b["amount"],
                            fill_id=b.get("fill_id")))
                    if parts == ["blacklist"]:
                        b = self._body()
                        return self._send(200, router.set_blacklist(
                            b["institution"], bool(b["blacklisted"])))
                    if len(parts) == 3 and parts[0] == "intents" and parts[2] == "cancel":
                        return self._send(200, router.cancel_intent(parts[1]))
                    if len(parts) == 3 and parts[0] == "intents" and parts[2] == "evaluate":
                        return self._send(200, router.evaluate_intent(parts[1]))
                    if len(parts) == 3 and parts[0] == "allocations" and parts[2] == "release":
                        return self._send(200, router.release_allocation(parts[1]))
                    if len(parts) == 3 and parts[0] == "institutions" and parts[2] == "freeze":
                        return self._send(200, router.freeze(parts[1]))
                    if len(parts) == 3 and parts[0] == "institutions" and parts[2] == "unfreeze":
                        return self._send(200, router.unfreeze(parts[1]))
                    if len(parts) == 3 and parts[0] == "institutions" and parts[2] == "limit":
                        b = self._body()
                        return self._send(200, router.set_limit(
                            parts[1], b.get("limit_total")))
                return self._send(404, {"error": "NOT_FOUND", "detail": self.path})
            except EngineError as exc:
                code = 404 if exc.code.endswith("NOT_FOUND") else 409
                return self._send(code, {"error": exc.code, "detail": exc.detail})
            except KeyError as exc:
                return self._send(400, {"error": "MISSING_FIELD", "detail": str(exc)})
            except ValueError as exc:
                return self._send(400, {"error": "BAD_REQUEST", "detail": str(exc)})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, *_args) -> None:
            pass

    return Handler


def make_server(port: int, router: QuoteRouter) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(router))


def run() -> None:
    router = QuoteRouter.open(os.getenv("DB_PATH", "./router.db"))
    port = int(os.getenv("PORT", "8000"))
    make_server(port, router).serve_forever()


if __name__ == "__main__":
    run()
