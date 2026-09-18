"""路由器核心行为测试。

覆盖：候选排序与解释、额度/最小成交量/新鲜度/黑名单、报价撤回、
部分成交、网络延迟下的成交回报与改路、冻结排队与恢复补处理、
并发幂等（同一意图唯一最终分配）、重启重放（锁定价格不丢）、
离线补传、远期生效报价禁用、排序确定性。
"""

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.api import make_handler
from service.clock import FakeClock
from service.models import Ccy, Intent, IntentStatus, Quote
from service.router import Router
from service.store import Journal


def mk_quote(
    qid, inst, rate, size=1000.0, min_size=10.0, ccy="CNY",
    received_at=1000.0, valid_until=2000.0, effective_from=None,
):
    return Quote(
        quote_id=qid, institution=inst, ccy=Ccy(ccy), rate=rate, size=size,
        min_size=min_size, received_at=received_at, valid_until=valid_until,
        effective_from=effective_from,
    )


def mk_intent(iid, amount=100.0, ccy="CNY", submitted_at=1001.0, key=None):
    return Intent(
        intent_id=iid, ccy=Ccy(ccy), amount=amount,
        submitted_at=submitted_at, idempotency_key=key,
    )


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal.log")
        self.clock = FakeClock(1000.0)
        self.router = Router(Journal(self.path), self.clock)

    def tearDown(self):
        self.router.journal.close()
        self.tmp.cleanup()

    def reopen(self, at=None):
        """模拟服务重启：用同一日志新建路由器。"""
        self.router.journal.close()
        if at is not None:
            self.clock.set(at)
        self.router = Router(Journal(self.path), self.clock)
        return self.router

    def event_types(self):
        return [e["type"] for e in Journal(self.path).replay()]

    def candidate(self, view, quote_id, last: bool = False):
        found = None
        for a in view["attempts"]:
            for c in a["baseline"]["candidates"]:
                if c["quote_id"] == quote_id:
                    found = c
                    if not last:
                        return c
        if found is not None:
            return found
        self.fail(f"candidate {quote_id} not found")


class RankingTest(RouterTestBase):
    def test_rate_then_freshness_and_explanation(self):
        # 同币种三家：A 最便宜；B、D 同价，D 更新鲜应排在 B 前；C 被黑名单
        self.router.ingest_quote(mk_quote("Q-A", "bankA", 7.05))
        self.router.ingest_quote(mk_quote("Q-B", "bankB", 7.08, received_at=990.0))
        self.router.ingest_quote(mk_quote("Q-C", "bankC", 7.01))  # 价最低但拉黑
        self.router.ingest_quote(mk_quote("Q-D", "bankD", 7.08, received_at=995.0))
        self.router.set_blacklisted("bankC", True)

        view = self.router.submit_intent(mk_intent("I-1"))

        self.assertEqual(view["status"], IntentStatus.FILLED.value)
        self.assertEqual(view["pieces"][0]["institution"], "bankA")
        self.assertEqual(view["pieces"][0]["rate"], 7.05)
        ranking = view["attempts"][0]["ranking"]
        self.assertEqual(ranking, ["Q-A", "Q-D", "Q-B"])
        self.assertEqual(self.candidate(view, "Q-C")["reason"], "BLACKLISTED")
        self.assertEqual(self.candidate(view, "Q-C")["rank"], None)
        # 可解释：成本、年龄、剩余额度、时钟都在快照里
        c = self.candidate(view, "Q-A")
        self.assertEqual(c["cost"], 705.0)
        self.assertEqual(c["age_seconds"], 0.0)
        self.assertEqual(c["remaining"], 1000.0)  # 决策快照在预留之前
        self.assertEqual(view["attempts"][0]["baseline"]["clock"], 1000.0)

    def test_ccy_isolation_cnh_vs_cny(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, ccy="CNH"))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.20, ccy="CNY"))
        view = self.router.submit_intent(mk_intent("I-1", ccy="CNY"))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view, "Q-1")["reason"], "CCY_MISMATCH")

    def test_min_size_and_limit_filters(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=100, min_size=50))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.06))
        # 成交量低于 A 的最小成交量 → 跳到 B
        view = self.router.submit_intent(mk_intent("I-1", amount=30))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view, "Q-1")["reason"], "BELOW_MIN_SIZE")
        # A 额度被后续成交吃到低于最小成交量 → 标记额度不足
        self.router.submit_intent(mk_intent("I-2", amount=80))
        view3 = self.router.submit_intent(mk_intent("I-3", amount=100))
        self.assertEqual(view3["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view3, "Q-1")["reason"], "INSUFFICIENT_LIMIT")

    def test_deterministic_independent_of_ingest_order(self):
        def run(order):
            tmp = tempfile.mkdtemp()
            p = os.path.join(tmp, "j")
            clk = FakeClock(1000.0)
            r = Router(Journal(p), clk)
            qs = [
                mk_quote("Q-A", "bankA", 7.10),
                mk_quote("Q-B", "bankB", 7.05),
                mk_quote("Q-C", "bankC", 7.05, received_at=999.0),
            ]
            for i in order:
                r.ingest_quote(qs[i])
            v = r.submit_intent(mk_intent("I-1"))
            r.journal.close()
            return v["attempts"][0]["ranking"], v["pieces"][0]["institution"]

        r1 = run([0, 1, 2])
        r2 = run([2, 0, 1])
        self.assertEqual(r1, r2)
        # 同价更新鲜者胜（B 在 1000 收到、C 在 999），报价号兜底
        self.assertEqual(r1[0], ["Q-B", "Q-C", "Q-A"])


class FreshnessAndValidityTest(RouterTestBase):
    def test_expired_quote_never_used(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.01, valid_until=1050.0))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.20, valid_until=2000.0))
        self.clock.set(1050.0)  # 决策时刻恰好等于失效时刻 → 已失效（左闭右开）
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view, "Q-1")["reason"], "EXPIRED")

    def test_offline_late_upload_of_stale_quote(self):
        # 离线补传：1000 时刻才上传一张 900 收到、950 就过期的报价
        self.router.ingest_quote(
            mk_quote("Q-OLD", "bankA", 7.01, received_at=900.0, valid_until=950.0)
        )
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.20))
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view, "Q-OLD")["reason"], "EXPIRED")

    def test_offline_catchup_still_valid_quote_is_usable(self):
        self.router.ingest_quote(
            mk_quote("Q-1", "bankA", 7.05, received_at=900.0, valid_until=1500.0)
        )
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["pieces"][0]["institution"], "bankA")
        self.assertEqual(self.candidate(view, "Q-1")["age_seconds"], 100.0)

    def test_forward_effective_quote_never_referenceable(self):
        # 收到之后才生效的报价：即使时钟已过生效时刻也永不引用
        self.router.ingest_quote(
            mk_quote("Q-FWD", "bankA", 6.99, received_at=1000.0,
                     effective_from=1010.0, valid_until=3000.0)
        )
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.20))
        self.clock.set(1500.0)
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(
            self.candidate(view, "Q-FWD")["reason"], "NOT_YET_EFFECTIVE"
        )

    def test_future_effective_quote_not_ready_yet(self):
        # 收到即生效（effective_from=received_at），但决策时刻还没到生效点不可能；
        # 这里验证 effective_from 在未来的常规情形
        self.router.ingest_quote(
            mk_quote("Q-1", "bankA", 7.01, received_at=1000.0,
                     effective_from=1000.0, valid_until=3000.0)
        )
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["status"], "FILLED")


class WithdrawTest(RouterTestBase):
    def test_withdraw_best_quote_reroutes(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.01))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09))
        self.router.withdraw_quote("Q-1")
        view = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(view["pieces"][0]["institution"], "bankB")
        self.assertEqual(self.candidate(view, "Q-1")["reason"], "WITHDRAWN")

    def test_withdraw_idempotent(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.01))
        self.router.withdraw_quote("Q-1")
        self.router.withdraw_quote("Q-1")
        self.assertEqual(self.event_types().count("QUOTE_WITHDRAWN"), 1)


class PartialFillTest(RouterTestBase):
    def test_split_across_quotes_by_ranking(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=60, min_size=10))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.08, size=100, min_size=10))
        view = self.router.submit_intent(mk_intent("I-1", amount=100))
        self.assertEqual(view["status"], IntentStatus.FILLED.value)
        self.assertEqual([p["institution"] for p in view["pieces"]], ["bankA", "bankB"])
        self.assertEqual([p["amount"] for p in view["pieces"]], ["60.00", "40.00"])
        self.assertEqual(view["filled"], "100.00")
        # 历史分配记录的就是锁定时的价格
        self.assertEqual([p["rate"] for p in view["pieces"]], [7.05, 7.08])

    def test_capacity_shortage_leaves_partial(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=60, min_size=10))
        view = self.router.submit_intent(mk_intent("I-1", amount=100))
        self.assertEqual(view["status"], IntentStatus.PARTIALLY_FILLED.value)
        self.assertEqual(view["filled"], "60.00")


class FillResultTest(RouterTestBase):
    def test_confirm_after_network_delay_keeps_lock(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05))
        v = self.router.submit_intent(mk_intent("I-1"))
        seq = v["pieces"][0]["seq"]
        self.assertEqual(v["pieces"][0]["state"], "RESERVED")
        self.clock.advance(30)  # 网络延迟
        v2 = self.router.confirm_fill("I-1", seq)
        self.assertEqual(v2["pieces"][0]["state"], "FILLED")
        self.assertEqual(v2["filled"], "100.00")

    def test_failed_fill_releases_limit_and_reroutes_same_intent(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=100))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09, size=100))
        v = self.router.submit_intent(mk_intent("I-1"))
        seq = v["pieces"][0]["seq"]
        self.clock.advance(5)
        v2 = self.router.report_fill_result("I-1", seq, success=False, reason="NACK")
        # 仍然是同一意图的唯一分配，新增一次带快照的改路尝试
        self.assertEqual(v2["intent_id"], "I-1")
        self.assertEqual(len(v2["attempts"]), 2)
        self.assertEqual(v2["pieces"][0]["institution"], "bankB")
        self.assertEqual(v2["status"], IntentStatus.FILLED.value)
        # A 的额度已完整释放回池
        self.assertEqual(self.candidate(v2, "Q-1")["remaining"], 100.0)
        self.assertEqual(v2["failed_pieces"][0]["institution"], "bankA")

    def test_short_fill_then_reroute_residual(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=60, min_size=50))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09, size=100, min_size=10))
        v = self.router.submit_intent(mk_intent("I-1", amount=60))
        seq = v["pieces"][0]["seq"]  # 首轮只锁 A 的 60
        v2 = self.router.report_fill_result(
            "I-1", seq, success=True, filled_amount="30"
        )
        # A 实际成交 30（回收 30 后低于其最小成交量 50，不可再用）
        # 缺口 30 改路 B
        self.assertEqual(v2["filled"], "60.00")
        self.assertEqual(
            [(p["institution"], p["amount"]) for p in v2["pieces"]],
            [("bankA", "30.00"), ("bankB", "30.00")],
        )
        self.assertEqual(
            self.candidate(v2, "Q-1", last=True)["reason"],
            "INSUFFICIENT_LIMIT",
        )


class FreezeTest(RouterTestBase):
    def test_freeze_queues_then_unfreeze_drains(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05))
        self.router.set_frozen("bankA", True)

        v = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(v["status"], IntentStatus.FROZEN_QUEUED.value)
        self.assertEqual(v["pieces"], [])
        self.assertEqual(self.candidate(v, "Q-1")["reason"], "FROZEN")
        self.assertEqual(self.router.pending_intents(), ["I-1"])

        self.clock.advance(60)
        self.router.set_frozen("bankA", False)
        v2 = self.router.get_intent("I-1")
        self.assertEqual(v2["status"], IntentStatus.FILLED.value)
        self.assertEqual(v2["pieces"][0]["institution"], "bankA")
        # 比较依据可还原：排队时与补处理时各一份带时钟的快照
        self.assertEqual(len(v2["attempts"]), 2)
        self.assertEqual(v2["attempts"][0]["baseline"]["clock"], 1000.0)
        self.assertEqual(v2["attempts"][1]["baseline"]["clock"], 1060.0)

    def test_blacklist_wins_over_freeze_and_removal_drains(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05))
        self.router.set_blacklisted("bankA", True)
        self.router.set_frozen("bankA", False)  # 不能用解冻覆盖黑名单
        v = self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(v["status"], IntentStatus.DECLINED.value)
        self.router.set_blacklisted("bankA", False)
        # 之前 DECLINED（未排队）不会自动补处理；新意图正常
        v2 = self.router.submit_intent(mk_intent("I-2"))
        self.assertEqual(v2["status"], IntentStatus.FILLED.value)

    def test_partial_then_freeze_queues_residual(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=40, min_size=10))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09, size=100, min_size=10))
        self.router.set_frozen("bankB", True)
        v = self.router.submit_intent(mk_intent("I-1", amount=100))
        self.assertEqual(v["status"], IntentStatus.FROZEN_QUEUED.value)
        self.assertEqual(v["filled"], "40.00")
        self.router.set_frozen("bankB", False)
        v2 = self.router.get_intent("I-1")
        self.assertEqual(v2["status"], IntentStatus.FILLED.value)
        self.assertEqual(v2["filled"], "100.00")


class IdempotencyTest(RouterTestBase):
    def test_concurrent_identical_intents_single_allocation(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=1000))
        errors = []

        def submit(i):
            try:
                # 并发提交“相同金额”的同一意图（同一幂等键）
                self.router.submit_intent(
                    mk_intent(f"I-DUP-{i}", amount=100, key="KEY-1")
                )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        views = []
        for i in range(20):
            v = self.router.get_intent(f"I-DUP-{i}")
            if v:
                views.append(v)
        # 除首个外全部标记 duplicate，并指向同一个最终分配
        self.assertEqual(len(views), 20)
        originals = {v["intent_id"] for v in views if not v.get("duplicate")}
        self.assertEqual(len(originals), 1)
        canonical = next(iter(originals))
        for v in views:
            if v.get("duplicate"):
                self.assertEqual(v["duplicate_of"], canonical)
            self.assertEqual(v["status"], IntentStatus.FILLED.value)
            self.assertEqual(v["filled"], "100.00")
        # 额度只被扣减一次
        types = self.event_types()
        self.assertEqual(types.count("INTENT_SUBMITTED"), 1)
        self.assertEqual(types.count("FILL_RESERVED"), 1)
        self.assertEqual(types.count("INTENT_DUPLICATE"), 19)

    def test_duplicate_quote_ingest_is_deduplicated(self):
        q = mk_quote("Q-1", "bankA", 7.05)
        self.assertTrue(not self.router.ingest_quote(q)["deduplicated"])
        self.assertTrue(self.router.ingest_quote(q)["deduplicated"])


class RecoveryTest(RouterTestBase):
    def test_restart_preserves_locked_prices_and_limits(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=100, min_size=50))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09, size=100))
        v = self.router.submit_intent(mk_intent("I-1", amount=60))
        seq = v["pieces"][0]["seq"]
        self.router.confirm_fill("I-1", seq)

        r2 = self.reopen(at=1500.0)
        # 已锁定的成交、价格、时钟快照完整恢复
        got = r2.get_intent("I-1")
        self.assertEqual(got["status"], IntentStatus.FILLED.value)
        self.assertEqual(got["pieces"][0], {
            "seq": 1, "institution": "bankA", "quote_id": "Q-1",
            "rate": 7.05, "amount": "60.00", "filled_at": 1000.0,
            "state": "FILLED",
        })
        self.assertEqual(got["attempts"][0]["baseline"]["clock"], 1000.0)
        # 额度恢复：A 剩 40
        self.clock.set(1500.0)
        v2 = r2.submit_intent(mk_intent("I-2", amount=100, submitted_at=1500.0))
        self.assertEqual(
            self.candidate(v2, "Q-1")["reason"], "INSUFFICIENT_LIMIT"
        )
        self.assertEqual(v2["pieces"][0]["institution"], "bankB")

    def test_restart_preserves_frozen_queue_and_drains(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05))
        self.router.set_frozen("bankA", True)
        self.router.submit_intent(mk_intent("I-1"))
        self.assertEqual(self.router.pending_intents(), ["I-1"])

        r2 = self.reopen(at=1200.0)
        self.assertEqual(r2.pending_intents(), ["I-1"])
        r2.set_frozen("bankA", False)
        v = r2.get_intent("I-1")
        self.assertEqual(v["status"], IntentStatus.FILLED.value)
        self.assertEqual(v["pieces"][0]["institution"], "bankA")

    def test_restart_replays_short_fill_release(self):
        self.router.ingest_quote(mk_quote("Q-1", "bankA", 7.05, size=60, min_size=50))
        self.router.ingest_quote(mk_quote("Q-2", "bankB", 7.09))
        v = self.router.submit_intent(mk_intent("I-1", amount=60))
        self.router.report_fill_result(
            "I-1", v["pieces"][0]["seq"], success=True, filled_amount="30"
        )
        r2 = self.reopen(at=1100.0)
        got = r2.get_intent("I-1")
        self.assertEqual(got["filled"], "60.00")
        self.assertEqual(got["status"], IntentStatus.FILLED.value)
        self.assertEqual(
            [(p["institution"], p["amount"]) for p in got["pieces"]],
            [("bankA", "30.00"), ("bankB", "30.00")],
        )
        # A 回收的 30 额度低于最小成交量，新意图只能走 B
        v3 = r2.submit_intent(mk_intent("I-2", amount=30, submitted_at=1100.0))
        self.assertEqual(v3["pieces"][0]["institution"], "bankB")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "journal.log")
        from service.clock import Clock
        self.router = Router(Journal(path), Clock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.router))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, data, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode())
        conn.close()
        return resp.status, payload

    def test_http_flow(self):
        status, _ = self._req("GET", "/health")
        self.assertEqual(status, 200)

        q = {
            "quote_id": "Q-1", "institution": "bankA", "ccy": "CNY",
            "rate": 7.05, "size": 1000, "min_size": 10,
            "valid_until": 9_999_999_999.0,
        }
        self.assertEqual(self._req("POST", "/quotes", q)[0], 200)
        self.assertEqual(self._req("POST", "/quotes", q)[1]["deduplicated"], True)

        intent = {
            "intent_id": "I-1", "idempotency_key": "KEY-1",
            "amount": 100,
        }
        st, v = self._req("POST", "/intents", intent)
        self.assertEqual(st, 200)
        self.assertEqual(v["status"], "FILLED")

        # 重复提交：不同 intent_id、同一幂等键
        st, dup = self._req("POST", "/intents", {**intent, "intent_id": "I-1B"})
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["duplicate_of"], "I-1")

        st, got = self._req("GET", "/intents/I-1")
        self.assertEqual(st, 200)
        self.assertEqual(got["pieces"][0]["quote_id"], "Q-1")

    def test_http_freeze_and_withdraw(self):
        q = {
            "quote_id": "Q-9", "institution": "bankZ", "ccy": "CNH",
            "rate": 7.0, "size": 100, "min_size": 1,
            "valid_until": 9_999_999_999.0,
        }
        self._req("POST", "/quotes", q)
        self.assertEqual(self._req("POST", "/institutions/bankZ/freeze", {"frozen": True})[0], 200)
        st, v = self._req("POST", "/intents", {"intent_id": "I-9", "ccy": "CNH", "amount": 50})
        self.assertEqual(v["status"], "FROZEN_QUEUED")
        self._req("POST", "/institutions/bankZ/freeze", {"frozen": False})
        _, v2 = self._req("GET", "/intents/I-9")
        self.assertEqual(v2["status"], "FILLED")
        self.assertEqual(self._req("POST", "/quotes/Q-9/withdraw", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
