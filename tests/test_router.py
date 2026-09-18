"""纯路由核心测试：排序、排除理由、有效期边界、贪心分配、确定性。"""
import unittest
from datetime import timedelta
from decimal import Decimal

from service.domain import QuoteStatus
from service.router import (
    BELOW_MIN_AMOUNT,
    BLACKLISTED,
    EXPIRED,
    FROZEN,
    LIMIT_EXHAUSTED,
    NOT_YET_EFFECTIVE,
    REMAINDER_BELOW_MIN,
    STALE,
    SUMMARY_FULL,
    SUMMARY_NONE,
    SUMMARY_PARTIAL,
    WITHDRAWN,
    RoutingConfig,
    evaluate,
)

from tests.helpers import BASE, PAIR, inst_state, make_quote


def run_eval(quotes, institutions=None, amount="100", now=BASE, config=None):
    return evaluate(
        pair=PAIR,
        amount=Decimal(amount),
        quotes=quotes,
        institutions=institutions or {},
        now=now,
        seq=1,
        mono=0.0,
        config=config or RoutingConfig(max_quote_age=None),
    )


def by_qid(payload):
    return {c["quote_id"]: c for c in payload["candidates"]}


class RankingTest(unittest.TestCase):
    def test_price_then_arrival_order(self):
        quotes = [
            make_quote("BankB", "Q2", price="7.11", received_seq=2),
            make_quote("BankA", "Q1", price="7.10", received_seq=3),
            make_quote("BankC", "Q3", price="7.10", received_seq=1),
        ]
        payload = run_eval(quotes)
        ranks = by_qid(payload)
        # 价格优先；同价时先到（received_seq 小）者优先
        self.assertEqual(ranks["Q3"]["rank"], 1)
        self.assertEqual(ranks["Q1"]["rank"], 2)
        self.assertEqual(ranks["Q2"]["rank"], 3)

    def test_ineligible_quotes_have_no_rank(self):
        quotes = [make_quote("BankA", "Q1"), make_quote("BankB", "Q2")]
        payload = run_eval(quotes, {"BankB": inst_state("BankB", frozen=True)})
        ranks = by_qid(payload)
        self.assertEqual(ranks["Q1"]["rank"], 1)
        self.assertIsNone(ranks["Q2"]["rank"])
        self.assertEqual(ranks["Q2"]["reasons"], [FROZEN])


class ExclusionReasonTest(unittest.TestCase):
    def test_all_reasons_reported_together(self):
        q = make_quote("BankA", "Q1", status=QuoteStatus.WITHDRAWN,
                       valid_until=BASE - timedelta(seconds=1))
        inst = {"BankA": inst_state("BankA", frozen=True, blacklisted=True)}
        reasons = by_qid(run_eval([q], inst))["Q1"]["reasons"]
        # 同一报价的多重排除理由全部记录，供审计还原
        self.assertIn(BLACKLISTED, reasons)
        self.assertIn(FROZEN, reasons)
        self.assertIn(WITHDRAWN, reasons)
        self.assertIn(EXPIRED, reasons)

    def test_below_min_amount(self):
        q = make_quote("BankA", "Q1", min_amount="500")
        reasons = by_qid(run_eval([q], amount="100"))["Q1"]["reasons"]
        self.assertIn(BELOW_MIN_AMOUNT, reasons)

    def test_limit_exhausted(self):
        q = make_quote("BankA", "Q1", min_amount="10")
        inst = {"BankA": inst_state("BankA", limit_total=Decimal("5"))}
        reasons = by_qid(run_eval([q], inst))["Q1"]["reasons"]
        self.assertIn(LIMIT_EXHAUSTED, reasons)

    def test_stale_quote(self):
        q = make_quote("BankA", "Q1", received_at=BASE - timedelta(seconds=61))
        config = RoutingConfig(max_quote_age=60.0)
        reasons = by_qid(run_eval([q], config=config))["Q1"]["reasons"]
        self.assertIn(STALE, reasons)

    def test_fresh_quote_passes(self):
        q = make_quote("BankA", "Q1", received_at=BASE - timedelta(seconds=59))
        config = RoutingConfig(max_quote_age=60.0)
        self.assertTrue(by_qid(run_eval([q], config=config))["Q1"]["eligible"])


class ValidityBoundaryTest(unittest.TestCase):
    """有效期为半开区间 [valid_from, valid_until)，边界行为固定。"""

    def test_effective_exactly_at_valid_from(self):
        q = make_quote("BankA", "Q1", valid_from=BASE)
        self.assertTrue(by_qid(run_eval([q], now=BASE))["Q1"]["eligible"])

    def test_not_yet_effective_quote_cannot_be_used(self):
        # “收到后才生效”的报价：生效前任何成交都不得引用它
        q = make_quote("BankA", "Q1", valid_from=BASE + timedelta(seconds=30))
        cand = by_qid(run_eval([q], now=BASE))["Q1"]
        self.assertFalse(cand["eligible"])
        self.assertIn(NOT_YET_EFFECTIVE, cand["reasons"])

    def test_expired_exactly_at_valid_until(self):
        q = make_quote("BankA", "Q1", valid_until=BASE)
        cand = by_qid(run_eval([q], now=BASE))["Q1"]
        self.assertFalse(cand["eligible"])
        self.assertIn(EXPIRED, cand["reasons"])

    def test_valid_one_second_before_expiry(self):
        q = make_quote("BankA", "Q1", valid_until=BASE)
        now = BASE - timedelta(seconds=1)
        self.assertTrue(by_qid(run_eval([q], now=now))["Q1"]["eligible"])


class AllocationTest(unittest.TestCase):
    def test_greedy_slicing_across_quotes(self):
        quotes = [
            make_quote("BankA", "Q1", price="7.10", max_amount="60"),
            make_quote("BankB", "Q2", price="7.11", max_amount="60"),
        ]
        payload = run_eval(quotes, amount="100")
        self.assertEqual(payload["summary"], SUMMARY_FULL)
        chosen = payload["chosen"]
        self.assertEqual(len(chosen), 2)
        self.assertEqual((chosen[0]["quote_id"], chosen[0]["amount"]), ("Q1", "60"))
        self.assertEqual((chosen[1]["quote_id"], chosen[1]["amount"]), ("Q2", "40"))

    def test_remainder_below_min_skips_quote(self):
        quotes = [
            make_quote("BankA", "Q1", price="7.10", max_amount="95"),
            make_quote("BankB", "Q2", price="7.11", min_amount="10"),
        ]
        payload = run_eval(quotes, amount="100")
        self.assertEqual(payload["summary"], SUMMARY_PARTIAL)
        cands = by_qid(payload)
        self.assertEqual(cands["Q2"]["skip_reason"], REMAINDER_BELOW_MIN)
        self.assertEqual(payload["allocated_total"], "95")

    def test_institution_limit_caps_slice(self):
        quotes = [make_quote("BankA", "Q1", max_amount="1000")]
        inst = {"BankA": inst_state("BankA", limit_total=Decimal("70"))}
        payload = run_eval(quotes, inst, amount="100")
        self.assertEqual(payload["summary"], SUMMARY_PARTIAL)
        self.assertEqual(payload["allocated_total"], "70")

    def test_no_eligible_quote(self):
        payload = run_eval([], amount="100")
        self.assertEqual(payload["summary"], SUMMARY_NONE)
        self.assertEqual(payload["chosen"], [])


class DeterminismTest(unittest.TestCase):
    def test_same_inputs_same_payload(self):
        quotes = [
            make_quote("BankB", "Q2", price="7.11", received_seq=2),
            make_quote("BankA", "Q1", price="7.10", received_seq=1),
        ]
        inst = {"BankA": inst_state("BankA"), "BankB": inst_state("BankB")}
        first = run_eval(quotes, inst)
        second = run_eval(list(reversed(quotes)), inst)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
