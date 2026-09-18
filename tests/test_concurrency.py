"""并发测试：相同金额并发提交、重复意图并发、并发成交、有效期穿越竞态。

引擎用单锁串行化所有命令，因此这些测试断言的是确定性结果，
而不是"概率上不出错"。
"""
import threading
import unittest
from datetime import timedelta
from decimal import Decimal

from service.domain import IntentStatus
from service.engine import EngineError

from tests.helpers import BASE, PAIR, make_engine, quote_dict


def run_threads(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results.append(fn(i))
        except Exception as exc:  # noqa: BLE001 - 测试需要收集一切异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, errors


class ConcurrentSameAmountTest(unittest.TestCase):
    def test_no_oversubscription_under_contention(self):
        engine, _ = make_engine()
        engine.set_limit("BankA", "1000")
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])

        results, errors = run_threads(
            16, lambda i: engine.submit_intent(f"K-{i}", PAIR, "100"))
        self.assertEqual(errors, [])

        allocated = [r for r in results if r["status"] == IntentStatus.ALLOCATED.value]
        pending = [r for r in results if r["status"] == IntentStatus.PENDING.value]
        # 1000 额度 / 每单 100：恰好 10 单足额锁定，其余未决
        self.assertEqual(len(allocated), 10)
        self.assertEqual(len(pending), 6)

        inst = engine.state_dump()["institutions"][0]
        self.assertEqual(inst["limit_reserved"], "1000")
        total_locked = sum(
            Decimal(a["amount"])
            for r in allocated for a in r["allocations"]
        )
        self.assertEqual(total_locked, Decimal("1000"))  # 永不超卖

    def test_duplicate_key_concurrent_single_allocation(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])

        results, errors = run_threads(
            8, lambda _i: engine.submit_intent("K-SAME", PAIR, "100"))
        self.assertEqual(errors, [])

        intent_ids = {r["intent_id"] for r in results}
        self.assertEqual(len(intent_ids), 1)  # 同一意图
        view = engine.get_intent(intent_ids.pop())
        # 只有一个最终分配：金额恰为 100，只路由过一次
        self.assertEqual(len(view["allocations"]), 1)
        self.assertEqual(view["allocations"][0]["amount"], "100")
        self.assertEqual(len(view["decisions"]), 1)


class ConcurrentFillTest(unittest.TestCase):
    def test_fills_converge_exactly(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        view = engine.submit_intent("K1", PAIR, "100")
        alloc_id = view["allocations"][0]["allocation_id"]

        _, errors = run_threads(
            10, lambda i: engine.record_fill(alloc_id, "10", fill_id=f"F-{i}"))
        self.assertEqual(errors, [])
        alloc = engine.get_allocation(alloc_id)
        self.assertEqual(alloc["filled"], "100")
        self.assertEqual(alloc["status"], "FILLED")
        self.assertEqual(engine.get_intent(view["intent_id"])["status"],
                         IntentStatus.FILLED.value)

        # 锁满后再成交必然失败，且状态不变
        with self.assertRaises(EngineError):
            engine.record_fill(alloc_id, "1")
        self.assertEqual(engine.get_allocation(alloc_id)["filled"], "100")

    def test_duplicate_fill_id_concurrent(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        view = engine.submit_intent("K1", PAIR, "100")
        alloc_id = view["allocations"][0]["allocation_id"]

        results, errors = run_threads(
            6, lambda _i: engine.record_fill(alloc_id, "10", fill_id="F-DUP"))
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for r in results if not r["deduplicated"]), 1)
        self.assertEqual(engine.get_allocation(alloc_id)["filled"], "10")


class ValidityRaceTest(unittest.TestCase):
    def test_decisions_never_reference_ineffective_quotes(self):
        """报价有效期被并发提交穿越时，每笔成交引用的报价
        在其决策时刻必须已生效且未过期。"""
        engine, clock = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict(
            "Q1", "7.10", valid_until=BASE + timedelta(seconds=2))])

        results, _ = run_threads(
            20, lambda i: engine.submit_intent(f"K-{i}", PAIR, "10"))
        clock.advance(5)  # 时钟穿越有效期
        results2, _ = run_threads(
            20, lambda i: engine.submit_intent(f"L-{i}", PAIR, "10"))

        from service.domain import parse_time
        for view in results + results2:
            for decision_id in view["decisions"]:
                payload = engine.get_decision(decision_id)["payload"]
                decided_at = parse_time(payload["clock"]["wall"])
                for slice_ in payload["chosen"]:
                    cand = next(c for c in payload["candidates"]
                                if c["quote_id"] == slice_["quote_id"])
                    self.assertLessEqual(parse_time(cand["valid_from"]), decided_at)
                    self.assertLess(decided_at, parse_time(cand["valid_until"]))

        # 穿越后提交的 20 单必然全部未决（报价已过期）
        self.assertTrue(all(
            r["status"] == IntentStatus.PENDING.value for r in results2))


if __name__ == "__main__":
    unittest.main()
