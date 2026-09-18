"""引擎行为测试：幂等、撤回、冻结补处理、黑名单、部分成交、
重启持久化、离线补传、有效期穿越、额度账本。"""
import unittest
from datetime import timedelta
from decimal import Decimal

from service.domain import AllocationStatus, IntentStatus
from service.engine import EngineError
from service.router import EXPIRED, FROZEN, NOT_YET_EFFECTIVE, RoutingConfig

from tests.helpers import BASE, PAIR, make_engine, quote_dict


class SubmitAndLockTest(unittest.TestCase):
    def test_full_allocation_and_decision_record(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        view = engine.submit_intent("K1", PAIR, "1000")

        self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
        self.assertEqual(len(view["allocations"]), 1)
        alloc = view["allocations"][0]
        self.assertEqual(alloc["price"], "7.10")
        self.assertEqual(alloc["amount"], "1000")

        # 决策记录：候选快照 + 理由 + 时钟信息，足以还原比较依据
        decision = engine.get_decision(view["decisions"][0])
        payload = decision["payload"]
        self.assertEqual(payload["summary"], "FULLY_ALLOCATED")
        self.assertEqual(payload["candidates"][0]["quote_id"], "Q1")
        self.assertEqual(payload["candidates"][0]["rank"], 1)
        self.assertIn("wall", payload["clock"])
        self.assertIn("seq", payload["clock"])

    def test_duplicate_submission_returns_same_intent(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        first = engine.submit_intent("K1", PAIR, "1000")
        second = engine.submit_intent("K1", PAIR, "1000")

        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["intent_id"], second["intent_id"])
        # 每笔订单只有一个最终分配：不会重复锁价
        self.assertEqual(len(second["allocations"]), 1)
        self.assertEqual(len(second["decisions"]), 1)

    def test_invalid_amount_rejected_deterministically(self):
        engine, _ = make_engine()
        bad = engine.submit_intent("K1", PAIR, "-5")
        self.assertEqual(bad["status"], IntentStatus.REJECTED.value)
        retry = engine.submit_intent("K1", PAIR, "-5")
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(retry["status"], IntentStatus.REJECTED.value)


class WithdrawTest(unittest.TestCase):
    def test_withdraw_excludes_future_but_keeps_locked(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        locked = engine.submit_intent("K1", PAIR, "100")
        alloc_id = locked["allocations"][0]["allocation_id"]

        res = engine.withdraw_quote("BankA", "Q1")
        self.assertEqual(res["status"], "WITHDRAWN")
        # 重复撤回结果确定
        self.assertEqual(engine.withdraw_quote("BankA", "Q1")["status"], "WITHDRAWN")

        # 已锁定的价格不受撤回影响，仍可成交
        fill = engine.record_fill(alloc_id, "100")
        self.assertEqual(fill["intent_status"], IntentStatus.FILLED.value)

        # 后续意图不再看到该报价
        view = engine.submit_intent("K2", PAIR, "100")
        self.assertEqual(view["status"], IntentStatus.PENDING.value)
        decision = engine.get_decision(view["decisions"][0])
        self.assertEqual(decision["payload"]["summary"], "NO_ELIGIBLE_QUOTE")


class FreezeTest(unittest.TestCase):
    def test_freeze_blocks_and_unfreeze_catches_up(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        engine.freeze("BankA")

        # 冻结期间意图保持未决
        pending = engine.submit_intent("K1", PAIR, "500")
        self.assertEqual(pending["status"], IntentStatus.PENDING.value)
        decision = engine.get_decision(pending["decisions"][0])
        self.assertEqual(decision["payload"]["candidates"][0]["reasons"], [FROZEN])

        # 解冻后自动补处理未决意图
        result = engine.unfreeze("BankA")
        self.assertTrue(result["triggered_decisions"])
        view = engine.get_intent(pending["intent_id"])
        self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
        self.assertEqual(view["allocations"][0]["price"], "7.10")

    def test_frozen_institution_locks_unaffected(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        locked = engine.submit_intent("K1", PAIR, "100")
        alloc_id = locked["allocations"][0]["allocation_id"]
        engine.freeze("BankA")
        fill = engine.record_fill(alloc_id, "100")
        self.assertEqual(fill["intent_status"], IntentStatus.FILLED.value)


class BlacklistTest(unittest.TestCase):
    def test_blacklist_excludes_and_removal_recovers(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        engine.set_blacklist("BankA", True)

        view = engine.submit_intent("K1", PAIR, "100")
        self.assertEqual(view["status"], IntentStatus.PENDING.value)

        result = engine.set_blacklist("BankA", False)
        self.assertTrue(result["triggered_decisions"])
        self.assertEqual(engine.get_intent(view["intent_id"])["status"],
                         IntentStatus.ALLOCATED.value)


class PartialFillTest(unittest.TestCase):
    def test_partial_fill_then_release_reroutes_remainder(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10", max_amount="100")])
        engine.ingest_snapshot("BankB", [quote_dict("Q2", "7.11", max_amount="100")])

        view = engine.submit_intent("K1", PAIR, "100")
        alloc_id = view["allocations"][0]["allocation_id"]
        self.assertEqual(view["allocations"][0]["institution"], "BankA")

        # 交易对手只成交 60 并撤回剩余报价，余量释放后自动改派 BankB
        fill = engine.record_fill(alloc_id, "60")
        self.assertEqual(fill["allocation_status"],
                         AllocationStatus.PARTIALLY_FILLED.value)
        engine.withdraw_quote("BankA", "Q1")
        engine.release_allocation(alloc_id)

        view = engine.get_intent(view["intent_id"])
        self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
        self.assertEqual(len(view["allocations"]), 2)
        second = view["allocations"][1]
        self.assertEqual(second["institution"], "BankB")
        self.assertEqual(second["amount"], "40")

        # 最终成交总额恰为请求量：一个最终分配序列
        engine.record_fill(second["allocation_id"], "40")
        view = engine.get_intent(view["intent_id"])
        self.assertEqual(view["status"], IntentStatus.FILLED.value)
        self.assertEqual(view["filled"], "100")

    def test_fill_idempotent_and_bounded(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        view = engine.submit_intent("K1", PAIR, "100")
        alloc_id = view["allocations"][0]["allocation_id"]

        first = engine.record_fill(alloc_id, "40", fill_id="F1")
        dup = engine.record_fill(alloc_id, "40", fill_id="F1")
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(engine.get_allocation(alloc_id)["filled"], "40")

        # 成交不得超过锁定余量
        with self.assertRaises(EngineError) as ctx:
            engine.record_fill(alloc_id, "61")
        self.assertEqual(ctx.exception.code, "FILL_EXCEEDS_LOCK")


class LimitLedgerTest(unittest.TestCase):
    def test_limit_reserved_then_used_then_released(self):
        engine, _ = make_engine()
        engine.set_limit("BankA", "100")
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])

        first = engine.submit_intent("K1", PAIR, "60")
        self.assertEqual(first["status"], IntentStatus.ALLOCATED.value)
        # 额度被预留后，第二个意图只能拿到剩余 40
        second = engine.submit_intent("K2", PAIR, "60")
        self.assertEqual(second["status"], IntentStatus.PARTIAL.value)
        self.assertEqual(second["allocations"][0]["amount"], "40")

        # 成交 60：预留转核销
        engine.record_fill(first["allocations"][0]["allocation_id"], "60")
        inst = engine.state_dump()["institutions"][0]
        self.assertEqual(inst["limit_used"], "60")
        self.assertEqual(inst["limit_reserved"], "40")

        # 撤销第二个意图：预留释放
        engine.cancel_intent(second["intent_id"])
        inst = engine.state_dump()["institutions"][0]
        self.assertEqual(inst["limit_reserved"], "0")


class RestartPersistenceTest(unittest.TestCase):
    def test_locked_prices_survive_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            engine, clock = make_engine(Path(tmp))
            engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
            locked = engine.submit_intent("K1", PAIR, "100")
            alloc_id = locked["allocations"][0]["allocation_id"]
            decision_id = locked["decisions"][0]
            engine.record_fill(alloc_id, "30")
            engine.close()

            # 模拟进程重启：同一数据库重新打开
            reopened, _ = make_engine(Path(tmp), clock=clock)
            alloc = reopened.get_allocation(alloc_id)
            self.assertEqual(alloc["price"], "7.10")   # 已锁价格不丢
            self.assertEqual(alloc["filled"], "30")

            view = reopened.get_intent(locked["intent_id"])
            # 30 已成交 + 70 仍锁定 = 足额覆盖，状态为 ALLOCATED
            self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
            self.assertEqual(view["filled"], "30")

            # 决策记录完整可回放
            decision = reopened.get_decision(decision_id)
            self.assertEqual(decision["payload"]["chosen"][0]["price"], "7.10")

            # 重启后继续成交同一锁定分配
            fill = reopened.record_fill(alloc_id, "70")
            self.assertEqual(fill["intent_status"], IntentStatus.FILLED.value)
            reopened.close()

    def test_pending_intent_reprocessed_after_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            engine, clock = make_engine(Path(tmp))
            engine.freeze("BankA")
            engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
            pending = engine.submit_intent("K1", PAIR, "100")
            self.assertEqual(pending["status"], IntentStatus.PENDING.value)
            engine.close()

            reopened, _ = make_engine(Path(tmp), clock=clock)
            reopened.unfreeze("BankA")   # 重启后解冻，补处理未决意图
            view = reopened.get_intent(pending["intent_id"])
            self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
            reopened.close()


class OfflineCatchupTest(unittest.TestCase):
    def test_late_backlog_never_uses_expired_quotes(self):
        engine, clock = make_engine()
        # 机构离线期间意图到达，无报价可用
        pending = engine.submit_intent("K1", PAIR, "100")
        self.assertEqual(pending["status"], IntentStatus.PENDING.value)

        # 离线补传：一张已过期、一张仍有效
        clock.advance(60)
        expired = quote_dict(
            "Q-OLD", "6.90",
            valid_from=BASE - timedelta(hours=2),
            valid_until=BASE - timedelta(hours=1),
        )
        alive = quote_dict("Q-NEW", "7.12")
        engine.ingest_snapshot("BankA", [expired, alive], snapshot_id="S1")

        view = engine.get_intent(pending["intent_id"])
        self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)
        # 只能用仍有效的报价，哪怕过期报价价格更好
        self.assertEqual(view["allocations"][0]["quote_id"], "Q-NEW")
        self.assertEqual(view["allocations"][0]["price"], "7.12")

        # 补传快照的决策里，过期报价的排除理由可审计
        last = engine.get_decision(view["decisions"][-1])
        cands = {c["quote_id"]: c for c in last["payload"]["candidates"]}
        self.assertIn(EXPIRED, cands["Q-OLD"]["reasons"])

    def test_late_snapshot_does_not_rewrite_history(self):
        engine, clock = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        done = engine.submit_intent("K1", PAIR, "100")
        decision_id = done["decisions"][0]

        # 更优报价迟到补传：历史决策不变
        clock.advance(30)
        engine.ingest_snapshot("BankB", [quote_dict("Q2", "7.05")])
        decision = engine.get_decision(decision_id)
        self.assertEqual(len(decision["payload"]["candidates"]), 1)
        self.assertEqual(decision["payload"]["chosen"][0]["quote_id"], "Q1")

    def test_snapshot_id_deduplicated(self):
        engine, _ = make_engine()
        first = engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")],
                                       snapshot_id="S1")
        dup = engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")],
                                     snapshot_id="S1")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(dup["deduplicated"])
        active = [q for q in engine.state_dump()["quotes"]
                  if q["status"] == "ACTIVE"]
        self.assertEqual(len(active), 1)


class ValidityCrossingTest(unittest.TestCase):
    def test_intent_crossing_quote_expiry(self):
        engine, clock = make_engine()
        soon_gone = quote_dict("Q1", "7.10",
                               valid_until=BASE + timedelta(seconds=30))
        engine.ingest_snapshot("BankA", [soon_gone])

        before = engine.submit_intent("K1", PAIR, "100")
        self.assertEqual(before["status"], IntentStatus.ALLOCATED.value)

        # 时钟越过有效期：同一报价不再可用
        clock.advance(31)
        after = engine.submit_intent("K2", PAIR, "100")
        self.assertEqual(after["status"], IntentStatus.PENDING.value)
        decision = engine.get_decision(after["decisions"][0])
        self.assertIn(EXPIRED, decision["payload"]["candidates"][0]["reasons"])

    def test_not_yet_effective_quote_unlocked_by_clock(self):
        # 关闭新鲜度阈值，隔离有效期语义
        engine, clock = make_engine(config=RoutingConfig(max_quote_age=None))
        future = quote_dict("Q1", "7.10",
                            valid_from=BASE + timedelta(seconds=60))
        engine.ingest_snapshot("BankA", [future])

        early = engine.submit_intent("K1", PAIR, "100")
        self.assertEqual(early["status"], IntentStatus.PENDING.value)
        decision = engine.get_decision(early["decisions"][0])
        self.assertIn(NOT_YET_EFFECTIVE,
                      decision["payload"]["candidates"][0]["reasons"])

        # 生效时刻之后重评估即可成交——成交不会引用生效前的报价
        clock.advance(61)
        view = engine.evaluate_intent(early["intent_id"])
        self.assertEqual(view["status"], IntentStatus.ALLOCATED.value)


class CancelTest(unittest.TestCase):
    def test_cancel_releases_and_is_terminal(self):
        engine, _ = make_engine()
        engine.ingest_snapshot("BankA", [quote_dict("Q1", "7.10")])
        view = engine.submit_intent("K1", PAIR, "100")
        engine.cancel_intent(view["intent_id"])
        view = engine.get_intent(view["intent_id"])
        self.assertEqual(view["status"], IntentStatus.CANCELLED.value)
        self.assertEqual(view["allocations"][0]["status"],
                         AllocationStatus.RELEASED.value)
        # 终态后重评估是确定的无操作
        again = engine.evaluate_intent(view["intent_id"])
        self.assertEqual(again["status"], IntentStatus.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
