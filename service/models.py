"""领域模型：报价、意图、状态枚举与分配结果。

时间字段语义
------------
``received_at``：路由器**收到**报价的时间（决策时的时钟信息）。
``effective_from``：报价可成交的起始时间。规则：任何成交不得引用
“收到之后才生效”的报价 —— 即 effective_from > received_at 的远期生效
报价在生效前不可选；生效后仍需在有效期内。
``valid_until``：报价失效时间（左闭右开），决策时间 >= valid_until 即过期。
``withdrawn_at``：机构撤回报价的时间，非 None 表示已撤回。
"""

from dataclasses import dataclass, field
from enum import Enum


class Ccy(Enum):
    CNY = "CNY"  # 在岸人民币
    CNH = "CNH"  # 离岸人民币


class QuoteStatus(Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"
    NOT_YET_EFFECTIVE = "NOT_YET_EFFECTIVE"
    BLACKLISTED = "BLACKLISTED"
    FROZEN = "FROZEN"
    INSUFFICIENT_LIMIT = "INSUFFICIENT_LIMIT"
    BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
    CCY_MISMATCH = "CCY_MISMATCH"
    FILL_REJECTED = "FILL_REJECTED"  # 该报价在本意图上回报失败，隔离改路


class IntentStatus(Enum):
    # 输入
    PENDING = "PENDING"
    FROZEN_QUEUED = "FROZEN_QUEUED"   # 候选机构被人工冻结，意图排队
    DECLINED = "DECLINED"             # 无可用候选（如全被黑名单拦截）
    # 成交状态机
    ALLOCATED = "ALLOCATED"           # 已锁定候选并预留额度
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"                 # 候选确认失败，等待改路
    # 幂等重复提交
    DUPLICATE = "DUPLICATE"


class InstitutionState(Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    BLACKLISTED = "BLACKLISTED"


@dataclass
class Quote:
    quote_id: str
    institution: str
    ccy: Ccy
    rate: float                 # 企业购汇价（每单位外币兑人民币报价币种金额）
    size: float                 # 原始报价量（外币金额）
    min_size: float             # 最小成交量
    valid_until: float
    received_at: float
    effective_from: float = None
    withdrawn_at: float = None

    def __post_init__(self):
        if self.effective_from is None:
            self.effective_from = self.received_at
        if not isinstance(self.ccy, Ccy):
            self.ccy = Ccy(self.ccy)

    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    def freshness_age(self, now: float) -> float:
        """报价年龄 = 决策时刻 - 收到时刻；越小越新鲜。"""
        return max(0.0, now - self.received_at)

    def to_snapshot(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "institution": self.institution,
            "ccy": self.ccy.value,
            "rate": self.rate,
            "size": self.size,
            "min_size": self.min_size,
            "valid_until": self.valid_until,
            "received_at": self.received_at,
            "effective_from": self.effective_from,
            "withdrawn_at": self.withdrawn_at,
        }

    @staticmethod
    def from_snapshot(d: dict) -> "Quote":
        d = dict(d)
        if isinstance(d.get("ccy"), str):
            d["ccy"] = Ccy(d["ccy"])
        return Quote(**d)


@dataclass
class Intent:
    intent_id: str
    ccy: Ccy
    amount: float               # 拟购汇金额（人民币，按报价币种 CNY/CNH 匹配）
    submitted_at: float
    idempotency_key: str = None
    duplicates: list = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.ccy, Ccy):
            self.ccy = Ccy(self.ccy)
        if self.idempotency_key is None:
            self.idempotency_key = self.intent_id

    def to_snapshot(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "ccy": self.ccy.value,
            "amount": self.amount,
            "submitted_at": self.submitted_at,
            "idempotency_key": self.idempotency_key,
            "duplicates": list(self.duplicates),
        }

    @staticmethod
    def from_snapshot(d: dict) -> "Intent":
        return Intent(**d)


@dataclass
class Allocation:
    """一笔意图的唯一最终分配（可含多个候选的部分成交）。"""
    intent_id: str
    pieces: list                # list[FillPiece]
    decided_at: float
    baseline_snapshot: dict     # 决策时全部候选的比较依据（原始报价+判定）
    status: IntentStatus
    chosen_institution: str = None
    reject_reasons: dict = field(default_factory=dict)

    @property
    def total_filled(self) -> float:
        return sum(p.amount for p in self.pieces)


@dataclass
class FillPiece:
    institution: str
    quote_id: str
    rate: float
    amount: float
    filled_at: float
    seq: int
    remaining_after: float = 0.0
