"""HTTP 接口冒烟测试：真实服务器 + urllib 往返。"""
import json
import threading
import unittest
import urllib.error
import urllib.request

from service.main import make_server

from tests.helpers import PAIR, make_engine, quote_dict


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine, _ = make_engine()
        cls.server = make_server(0, engine)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        code, health = self.call("GET", "/health")
        self.assertEqual((code, health["status"]), (200, "ok"))

        code, snap = self.call("POST", "/quotes/snapshot", {
            "institution": "BankA",
            "snapshot_id": "S-HTTP-1",
            "quotes": [quote_dict("Q1", "7.10")],
        })
        self.assertEqual(code, 200)
        self.assertEqual(len(snap["accepted"]), 1)

        code, intent = self.call("POST", "/intents", {
            "idempotency_key": "K-HTTP-1", "ccy_pair": PAIR, "amount": "500",
        })
        self.assertEqual(code, 200)
        self.assertEqual(intent["status"], "ALLOCATED")

        # 重复提交：同一意图，同一最终分配
        code, dup = self.call("POST", "/intents", {
            "idempotency_key": "K-HTTP-1", "ccy_pair": PAIR, "amount": "500",
        })
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(dup["intent_id"], intent["intent_id"])

        # 决策记录可通过 HTTP 取回，含时钟与理由
        code, decision = self.call("GET", f"/decisions/{intent['decisions'][0]}")
        self.assertEqual(code, 200)
        self.assertIn("clock", decision["payload"])
        self.assertEqual(decision["payload"]["candidates"][0]["rank"], 1)

        # 部分成交 + 查询
        alloc_id = intent["allocations"][0]["allocation_id"]
        code, fill = self.call("POST", "/fills", {
            "allocation_id": alloc_id, "amount": "200", "fill_id": "F-HTTP-1",
        })
        self.assertEqual(code, 200)
        self.assertEqual(fill["allocation_status"], "PARTIALLY_FILLED")

        code, view = self.call("GET", f"/intents/{intent['intent_id']}")
        self.assertEqual(view["filled"], "200")

    def test_error_mapping(self):
        code, body = self.call("GET", "/intents/NOPE")
        self.assertEqual((code, body["error"]), (404, "INTENT_NOT_FOUND"))

        code, body = self.call("POST", "/fills", {
            "allocation_id": "NOPE", "amount": "1",
        })
        self.assertEqual(code, 404)

        code, _body = self.call("POST", "/no/such/route", {})
        self.assertEqual(code, 404)

    def test_freeze_unfreeze_over_http(self):
        self.call("POST", "/quotes/snapshot", {
            "institution": "BankF", "quotes": [quote_dict("QF", "7.20")],
        })
        self.call("POST", "/institutions/BankF/freeze")
        code, intent = self.call("POST", "/intents", {
            "idempotency_key": "K-HTTP-2", "ccy_pair": PAIR, "amount": "10",
        })
        self.assertEqual(intent["status"], "PENDING")
        code, _ = self.call("POST", "/institutions/BankF/unfreeze")
        code, view = self.call("GET", f"/intents/{intent['intent_id']}")
        self.assertEqual(view["status"], "ALLOCATED")


if __name__ == "__main__":
    unittest.main()
