"""纯路由核心。

evaluate() 是纯函数：给定状态快照与时钟读数，输出确定性的
候选评价（每条报价的选用/排除理由）与分配方案。不做任何 I/O，
不读取系统时钟，因此同样的输入永远得到同样的输出——
决策记录落库后，任何时候都能用同一份输入重放比较依据。

排序规则（全部确定性，无随机、无字典序依赖）：
  1. 价格升序（购汇方买价越低越好）；
  2. 接收序号升序（先到的报价优先，FIFO）；
  3. 报价 ID 升序（兜底，保证全序）。

有效期判定（半开区间，边界行为固定并写入决策记录）：
  生效当且仅当 valid_from <= now < valid_until。
  因此"收到后才生效"的报价（valid_from > now）在生效前一律排除，
  任何成交都不可能引用它。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .clock import ensure_utc
from .domain import (
    InstitutionState,
    Quote,
    QuoteStatus,
    fmt_time,
)

# 排除/跳过原因码（写入决策记录，供事后审计）
OK = "OK"
BLACKLISTED = "BLACKLISTED"                # 合规黑名单
FROZEN = "FROZEN"                          # 人工冻结
WITHDRAWN = "WITHDRAWN"                    # 已撤回/被替换
NOT_YET_EFFECTIVE = "NOT_YET_EFFECTIVE"    # 收到后才生效，尚不可引用
EXPIRED = "EXPIRED"                        # 已过有效期
STALE = "STALE"                            # 超过新鲜度阈值
BELOW_MIN_AMOUNT = "BELOW_MIN_AMOUNT"      # 请求量低于该报价最小成交量
LIMIT_EXHAUSTED = "LIMIT_EXHAUSTED"        # 机构可用额度不足
REMAINDER_BELOW_MIN = "REMAINDER_BELOW_MIN"  # 分配到这里时剩余量已低于最小成交量

SUMMARY_FULL = "FULLY_ALLOCATED"
SUMMARY_PARTIAL = "PARTIALLY_ALLOCATED"
SUMMARY_NONE = "NO_ELIGIBLE_QUOTE"


@dataclass(frozen=True)
class RoutingConfig:
    """路由参数。max_quote_age 为新鲜度阈值（秒），None 表示不限制。"""

    max_quote_age: Optional[float] = 30.0


def _exclusion_reasons(
    quote: Quote,
    inst: Optional[InstitutionState],
    amount: Decimal,
    now: datetime,
    config: RoutingConfig,
) -> list[str]:
    """汇总一条报价的全部排除理由（空列表表示合格）。

    所有适用的理由都会记录，而不是只报第一条——
    这样审计时能看到完整的比较依据。
    """
    reasons: list[str] = []
    if inst is not None and inst.blacklisted:
        reasons.append(BLACKLISTED)
    if inst is not None and inst.frozen:
        reasons.append(FROZEN)
    if quote.status is not QuoteStatus.ACTIVE:
        reasons.append(WITHDRAWN)
    if now < quote.valid_from:
        reasons.append(NOT_YET_EFFECTIVE)
    if now >= quote.valid_until:
        reasons.append(EXPIRED)
    if config.max_quote_age is not None:
        age = (now - quote.received_at).total_seconds()
        if age > config.max_quote_age:
            reasons.append(STALE)
    if amount < quote.min_amount:
        reasons.append(BELOW_MIN_AMOUNT)
    if inst is not None:
        remaining = inst.remaining_limit()
        if remaining is not None and remaining < quote.min_amount:
            reasons.append(LIMIT_EXHAUSTED)
    return reasons


def evaluate(
    *,
    pair: str,
    amount: Decimal,
    quotes: list[Quote],
    institutions: dict[str, InstitutionState],
    now: datetime,
    seq: int,
    mono: float,
    config: RoutingConfig,
) -> dict:
    """对一次路由请求求值，返回决策载荷（可直接 JSON 序列化）。

    quotes 只需包含该货币对的报价；institutions 提供冻结/黑名单/额度。
    返回结构：
      clock:     决策时钟信息（墙钟/单调钟/逻辑序号）
      candidates: 每条报价的快照 + 合格性 + 理由 + 名次 + 实际分配
      chosen:    命中的分配切片（按名次顺序）
      summary:   FULLY_ALLOCATED / PARTIALLY_ALLOCATED / NO_ELIGIBLE_QUOTE
    """
    now = ensure_utc(now)

    candidates: list[dict] = []
    for q in quotes:
        inst = institutions.get(q.institution)
        reasons = _exclusion_reasons(q, inst, amount, now, config)
        candidates.append(
            {
                "institution": q.institution,
                "quote_id": q.quote_id,
                "ccy_pair": q.ccy_pair,
                "price": str(q.price),
                "min_amount": str(q.min_amount),
                "max_amount": str(q.max_amount),
                "valid_from": fmt_time(q.valid_from),
                "valid_until": fmt_time(q.valid_until),
                "received_at": fmt_time(q.received_at),
                "received_seq": q.received_seq,
                "status": q.status.value,
                "institution_frozen": bool(inst.frozen) if inst else False,
                "institution_blacklisted": bool(inst.blacklisted) if inst else False,
                "eligible": not reasons,
                "reasons": reasons,
                "rank": None,
                "allocated": None,
                "skip_reason": None,
            }
        )

    eligible = [c for c in candidates if c["eligible"]]
    ineligible = [c for c in candidates if not c["eligible"]]

    # 确定性排序：价格 → 接收序号 → 报价ID
    eligible.sort(key=lambda c: (Decimal(c["price"]), c["received_seq"], c["quote_id"]))
    ineligible.sort(key=lambda c: (c["institution"], c["quote_id"]))
    for rank, cand in enumerate(eligible, start=1):
        cand["rank"] = rank

    # 贪心分配：按名次依次切片，受单票上限与机构剩余额度约束
    limit_left = {
        name: st.remaining_limit() for name, st in institutions.items()
    }
    needed = amount
    chosen: list[dict] = []
    for cand in eligible:
        if needed <= 0:
            break
        q_min = Decimal(cand["min_amount"])
        q_max = Decimal(cand["max_amount"])
        cap = q_max
        inst_remaining = limit_left.get(cand["institution"])
        if inst_remaining is not None:
            cap = min(cap, inst_remaining)
        take = min(needed, cap)
        if take < q_min:
            cand["skip_reason"] = (
                REMAINDER_BELOW_MIN if needed < q_min else LIMIT_EXHAUSTED
            )
            continue
        cand["allocated"] = str(take)
        chosen.append(
            {
                "institution": cand["institution"],
                "quote_id": cand["quote_id"],
                "amount": str(take),
                "price": cand["price"],
                "rank": cand["rank"],
            }
        )
        needed -= take
        if inst_remaining is not None:
            limit_left[cand["institution"]] = inst_remaining - take

    allocated_total = amount - needed
    if allocated_total <= 0:
        summary = SUMMARY_NONE
    elif needed <= 0:
        summary = SUMMARY_FULL
    else:
        summary = SUMMARY_PARTIAL

    return {
        "clock": {
            "wall": fmt_time(now),
            "mono": mono,
            "seq": seq,
            "validity_rule": "valid_from <= now < valid_until",
        },
        "requested": str(amount),
        "ccy_pair": pair,
        "candidates": eligible + ineligible,
        "chosen": chosen,
        "allocated_total": str(allocated_total),
        "unallocated": str(needed),
        "summary": summary,
    }
