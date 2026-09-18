"""领域模型。

约定：
- 金额、价格一律使用 Decimal，JSON 序列化为字符串，避免浮点误差；
- 时间一律为 UTC，序列化为 ISO 字符串；
- 所有状态枚举显式持久化，保证重启后可还原。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional

from .clock import ensure_utc


class QuoteStatus(str, Enum):
    ACTIVE = "ACTIVE"          # 可参与路由
    WITHDRAWN = "WITHDRAWN"    # 机构主动撤回
    REPLACED = "REPLACED"      # 被同机构更新的快照替换


class IntentStatus(str, Enum):
    PENDING = "PENDING"        # 待路由：当前没有可用报价覆盖
    ALLOCATED = "ALLOCATED"    # 已足额锁定（含已成交部分），等待成交回报
    PARTIAL = "PARTIAL"        # 部分锁定或部分成交，剩余部分仍待路由
    FILLED = "FILLED"          # 全部成交（终态）
    CANCELLED = "CANCELLED"    # 人工撤销（终态）
    REJECTED = "REJECTED"      # 参数非法（终态）


class AllocationStatus(str, Enum):
    LOCKED = "LOCKED"                      # 已锁定价格，未成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交
    FILLED = "FILLED"                      # 全部成交（终态）
    RELEASED = "RELEASED"                  # 未成交部分被释放（终态）


# 终态集合：进入后不再参与任何重评估
TERMINAL_INTENT_STATES = {IntentStatus.FILLED, IntentStatus.CANCELLED, IntentStatus.REJECTED}
ACTIVE_ALLOCATION_STATES = {AllocationStatus.LOCKED, AllocationStatus.PARTIALLY_FILLED}


def to_decimal(value: Any) -> Decimal:
    """把 JSON 输入（数字或字符串）规范化为 Decimal。"""
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"无法解析为数值: {value!r}")


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return ensure_utc(datetime.fromisoformat(text))
    raise ValueError(f"无法解析为时间: {value!r}")


def fmt_time(ts: datetime) -> str:
    return ensure_utc(ts).isoformat()


@dataclass
class Quote:
    """一条机构报价。received_at/received_seq 由服务端在接收时打上，
    任何成交不得引用 received_seq 晚于决策序号的报价（由引擎天然保证：
    决策只读取当时已入库的报价）。"""

    institution: str
    quote_id: str
    ccy_pair: str
    price: Decimal
    min_amount: Decimal
    max_amount: Decimal
    valid_from: datetime
    valid_until: datetime
    received_at: datetime
    received_seq: int
    status: QuoteStatus = QuoteStatus.ACTIVE

    @property
    def key(self) -> tuple[str, str]:
        return (self.institution, self.quote_id)

    def to_dict(self) -> dict:
        return {
            "institution": self.institution,
            "quote_id": self.quote_id,
            "ccy_pair": self.ccy_pair,
            "price": str(self.price),
            "min_amount": str(self.min_amount),
            "max_amount": str(self.max_amount),
            "valid_from": fmt_time(self.valid_from),
            "valid_until": fmt_time(self.valid_until),
            "received_at": fmt_time(self.received_at),
            "received_seq": self.received_seq,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Quote":
        return cls(
            institution=data["institution"],
            quote_id=data["quote_id"],
            ccy_pair=data["ccy_pair"],
            price=to_decimal(data["price"]),
            min_amount=to_decimal(data["min_amount"]),
            max_amount=to_decimal(data["max_amount"]),
            valid_from=parse_time(data["valid_from"]),
            valid_until=parse_time(data["valid_until"]),
            received_at=parse_time(data["received_at"]),
            received_seq=int(data["received_seq"]),
            status=QuoteStatus(data["status"]),
        )


@dataclass
class InstitutionState:
    """机构运行状态：冻结/黑名单标记与额度账本。
    额度三段式：limit_total = used(已成交) + reserved(已锁定未成交) + 可用。"""

    name: str
    frozen: bool = False
    blacklisted: bool = False
    limit_total: Optional[Decimal] = None   # None 表示不限额
    limit_used: Decimal = Decimal(0)
    limit_reserved: Decimal = Decimal(0)

    def remaining_limit(self) -> Optional[Decimal]:
        if self.limit_total is None:
            return None
        return self.limit_total - self.limit_used - self.limit_reserved

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "frozen": self.frozen,
            "blacklisted": self.blacklisted,
            "limit_total": None if self.limit_total is None else str(self.limit_total),
            "limit_used": str(self.limit_used),
            "limit_reserved": str(self.limit_reserved),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstitutionState":
        return cls(
            name=data["name"],
            frozen=bool(data["frozen"]),
            blacklisted=bool(data["blacklisted"]),
            limit_total=None if data["limit_total"] is None else to_decimal(data["limit_total"]),
            limit_used=to_decimal(data["limit_used"]),
            limit_reserved=to_decimal(data["limit_reserved"]),
        )


@dataclass
class Intent:
    """企业购汇意图。idempotency_key 保证重复提交只产生一个意图。"""

    intent_id: str
    idempotency_key: str
    ccy_pair: str
    amount: Decimal
    filled: Decimal = Decimal(0)
    status: IntentStatus = IntentStatus.PENDING
    created_at: datetime = None  # type: ignore[assignment]
    created_seq: int = 0
    reject_reason: Optional[str] = None
    version: int = 0             # 每次状态迁移递增，便于并发观测

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "ccy_pair": self.ccy_pair,
            "amount": str(self.amount),
            "filled": str(self.filled),
            "status": self.status.value,
            "created_at": fmt_time(self.created_at),
            "created_seq": self.created_seq,
            "reject_reason": self.reject_reason,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Intent":
        return cls(
            intent_id=data["intent_id"],
            idempotency_key=data["idempotency_key"],
            ccy_pair=data["ccy_pair"],
            amount=to_decimal(data["amount"]),
            filled=to_decimal(data["filled"]),
            status=IntentStatus(data["status"]),
            created_at=parse_time(data["created_at"]),
            created_seq=int(data["created_seq"]),
            reject_reason=data.get("reject_reason"),
            version=int(data.get("version", 0)),
        )


@dataclass
class Allocation:
    """一次路由决策锁定的价格切片。锁定后即持久化，
    服务重启、报价撤回或过期都不影响已锁定的价格。"""

    allocation_id: str
    intent_id: str
    institution: str
    quote_id: str
    amount: Decimal              # 锁定数量
    price: Decimal               # 锁定价格
    decision_id: str
    filled: Decimal = Decimal(0)
    status: AllocationStatus = AllocationStatus.LOCKED
    created_seq: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.filled

    def to_dict(self) -> dict:
        return {
            "allocation_id": self.allocation_id,
            "intent_id": self.intent_id,
            "institution": self.institution,
            "quote_id": self.quote_id,
            "amount": str(self.amount),
            "price": str(self.price),
            "decision_id": self.decision_id,
            "filled": str(self.filled),
            "status": self.status.value,
            "created_seq": self.created_seq,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Allocation":
        return cls(
            allocation_id=data["allocation_id"],
            intent_id=data["intent_id"],
            institution=data["institution"],
            quote_id=data["quote_id"],
            amount=to_decimal(data["amount"]),
            price=to_decimal(data["price"]),
            decision_id=data["decision_id"],
            filled=to_decimal(data["filled"]),
            status=AllocationStatus(data["status"]),
            created_seq=int(data.get("created_seq", 0)),
        )


@dataclass
class Decision:
    """路由决策记录：保存原始报价快照、逐条选用/排除理由与时钟信息，
    足以在事后还原当时的比较依据。payload 结构见 router.evaluate。"""

    decision_id: str
    intent_id: str
    seq: int
    payload: dict

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "seq": self.seq,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        return cls(
            decision_id=data["decision_id"],
            intent_id=data["intent_id"],
            seq=int(data["seq"]),
            payload=data["payload"],
        )
