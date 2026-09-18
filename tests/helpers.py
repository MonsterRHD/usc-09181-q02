"""测试共享辅助。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from service.clock import ManualClock
from service.domain import InstitutionState, Quote, to_decimal
from service.engine import QuoteRouter
from service.router import RoutingConfig

BASE = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)
PAIR = "USD/CNH"


def make_engine(tmp_path=None, config=None, clock=None):
    """构造引擎：默认手动时钟 + 内存库；tmp_path 给出时用文件库以测重启。"""
    clock = clock or ManualClock(BASE)
    db = str(tmp_path / "router.db") if tmp_path is not None else ":memory:"
    return QuoteRouter.open(db, clock=clock, config=config), clock


def quote_dict(
    quote_id,
    price,
    min_amount="1",
    max_amount="1000000",
    valid_from=None,
    valid_until=None,
    pair=PAIR,
):
    return {
        "quote_id": quote_id,
        "ccy_pair": pair,
        "price": str(price),
        "min_amount": str(min_amount),
        "max_amount": str(max_amount),
        "valid_from": (valid_from or BASE - timedelta(minutes=5)).isoformat(),
        "valid_until": (valid_until or BASE + timedelta(minutes=5)).isoformat(),
    }


def make_quote(
    inst="BankA",
    qid="Q1",
    price="7.10",
    min_amount="1",
    max_amount="1000000",
    received_at=None,
    received_seq=1,
    valid_from=None,
    valid_until=None,
    pair=PAIR,
    status=None,
):
    from service.domain import QuoteStatus

    return Quote(
        institution=inst,
        quote_id=qid,
        ccy_pair=pair,
        price=to_decimal(price),
        min_amount=to_decimal(min_amount),
        max_amount=to_decimal(max_amount),
        valid_from=valid_from or BASE - timedelta(minutes=5),
        valid_until=valid_until or BASE + timedelta(minutes=5),
        received_at=received_at or BASE - timedelta(seconds=5),
        received_seq=received_seq,
        status=status or QuoteStatus.ACTIVE,
    )


def inst_state(name, **kw):
    return InstitutionState(name=name, **kw)
