"""HTTP 适配层：把 JSON 快照转成领域命令。

离线补传：``received_at`` / ``submitted_at`` / ``valid_until`` 等时间
全部允许由调用方在报文里携带（补传场景），缺省才使用服务端时钟。
路由状态保存在 JOURNAL_PATH（默认 data/router.journal），重启自动重放。
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .clock import Clock
from .models import Ccy, Intent, Quote
from .router import Router
from .store import Journal


def build_router(journal_path: str = None) -> Router:
    journal_path = journal_path or os.getenv("JOURNAL_PATH", "data/router.journal")
    return Router(Journal(journal_path), Clock())


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
                return
            if self.path.startswith("/intents/"):
                view = router.get_intent(intent_id=self.path.rsplit("/", 1)[-1])
                self._send(200 if view else 404, view or {"error": "not found"})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                self._route_post()
            except KeyError as e:
                self._send(404, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})

        def _route_post(self):
            p = self.path
            data = self._read_json()
            now = router.clock.now()

            if p == "/quotes":
                data.setdefault("received_at", now)
                q = Quote.from_snapshot(data)
                self._send(200, router.ingest_quote(q))
                return

            if p.startswith("/quotes/") and p.endswith("/withdraw"):
                qid = p.split("/")[2]
                self._send(200, router.withdraw_quote(qid, at=data.get("at")))
                return

            if p.startswith("/institutions/") and p.endswith("/freeze"):
                name = p.split("/")[2]
                router.set_frozen(name, bool(data.get("frozen", True)))
                self._send(200, {"institution": name, "frozen": data.get("frozen", True)})
                return

            if p.startswith("/institutions/") and p.endswith("/blacklist"):
                name = p.split("/")[2]
                router.set_blacklisted(name, bool(data.get("blacklisted", True)))
                self._send(200, {"institution": name, "blacklisted": data.get("blacklisted", True)})
                return

            if p == "/intents":
                data.setdefault("submitted_at", now)
                data.setdefault("ccy", "CNY")
                intent = Intent.from_snapshot(data)
                self._send(200, router.submit_intent(intent))
                return

            if p.startswith("/intents/") and "/fills/" in p:
                parts = p.strip("/").split("/")
                intent_id, seq = parts[1], int(parts[3])
                if p.endswith("/confirm"):
                    self._send(200, router.confirm_fill(intent_id, seq))
                elif p.endswith("/result"):
                    self._send(
                        200,
                        router.report_fill_result(
                            intent_id,
                            seq,
                            success=bool(data.get("success", True)),
                            filled_amount=data.get("filled_amount"),
                            reason=data.get("reason"),
                        ),
                    )
                else:
                    self._send(404, {"error": "not found"})
                return

            self._send(404, {"error": "not found"})

    return Handler


def create_server(host: str = "0.0.0.0", port: int = None, journal_path: str = None):
    router = build_router(journal_path)
    port = port or int(os.getenv("PORT", "8000"))
    return ThreadingHTTPServer((host, port), make_handler(router))


def run():  # pragma: no cover
    create_server().serve_forever()
