"""报价路由引擎。

线程模型：所有公开方法在同一把可重入锁内执行，命令串行化，
因此并发提交（相同金额、相同幂等键、并发成交回报）都有确定结果。

持久化：每次命令在一个事务内写穿到 SQLite 并追加事件日志；
QuoteRouter.open() 可在进程重启后完整恢复——已锁定的价格、
未决意图、额度账本、幂等键都不会丢失。

核心不变量：
1. 任何成交只引用决策时刻已接收、已生效（valid_from <= now < valid_until）、
   未撤回的报价；决策记录保存全部候选快照与理由，可重放比较依据。
2. 同一幂等键只产生一个意图，每笔订单只有一个最终分配序列。
3. 机构额度三段账本（used + reserved <= limit_total），锁定即预留，
   成交转核销，释放/撤销回滚预留——并发下永不超卖。
4. 冻结/黑名单/撤回只影响后续决策，不影响已锁定的分配。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from .clock import Clock, SystemClock
from .domain import (
    ACTIVE_ALLOCATION_STATES,
    TERMINAL_INTENT_STATES,
    Allocation,
    AllocationStatus,
    Decision,
    InstitutionState,
    Intent,
    IntentStatus,
    Quote,
    QuoteStatus,
    fmt_time,
    parse_time,
    to_decimal,
)
from .router import RoutingConfig, evaluate
from .store import Store


class EngineError(Exception):
    """业务冲突（如成交超额、对象不存在）。code 用于 HTTP 映射。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class QuoteRouter:
    def __init__(
        self,
        store: Store,
        clock: Optional[Clock] = None,
        config: Optional[RoutingConfig] = None,
    ):
        self._store = store
        self._clock = clock or SystemClock()
        self._config = config or RoutingConfig()
        self._lock = threading.RLock()

        self._quotes: dict[tuple[str, str], Quote] = {}
        self._institutions: dict[str, InstitutionState] = {}
        self._intents: dict[str, Intent] = {}
        self._allocations: dict[str, Allocation] = {}
        self._decisions: dict[str, Decision] = {}
        self._idem_index: dict[str, str] = {}          # 幂等键 -> intent_id
        self._seen_snapshots: set[str] = set()
        self._seen_fills: dict[str, dict] = {}         # fill_id -> 成交回执
        self._seq = 0
        self._load()

    # ------------------------------------------------------------------
    # 启动与恢复
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        db_path: str,
        clock: Optional[Clock] = None,
        config: Optional[RoutingConfig] = None,
    ) -> "QuoteRouter":
        return cls(Store(db_path), clock=clock, config=config)

    def close(self) -> None:
        self._store.close()

    def _load(self) -> None:
        """从存储恢复全部状态（重启恢复入口）。"""
        with self._lock:
            for data in self._store.load_table("quotes"):
                q = Quote.from_dict(data)
                self._quotes[q.key] = q
            for data in self._store.load_table("institutions"):
                st = InstitutionState.from_dict(data)
                self._institutions[st.name] = st
            for data in self._store.load_table("intents"):
                it = Intent.from_dict(data)
                self._intents[it.intent_id] = it
                self._idem_index[it.idempotency_key] = it.intent_id
            for data in self._store.load_table("allocations"):
                a = Allocation.from_dict(data)
                self._allocations[a.allocation_id] = a
            for data in self._store.load_table("decisions"):
                d = Decision.from_dict(data)
                self._decisions[d.decision_id] = d
            self._seq = int(self._store.get_meta("seq") or 0)
            self._seen_snapshots = set(
                (self._store.get_meta("seen_snapshots") or "").split("\n")
            ) - {""}
            raw_fills = self._store.get_meta("seen_fills")
            if raw_fills:
                self._seen_fills = json.loads(raw_fills)

    def _next_seq(self) -> int:
        self._seq += 1
        self._store.set_meta("seq", str(self._seq))
        return self._seq

    def _event(self, type_: str, payload: dict) -> None:
        seq = self._next_seq()
        self._store.append_event(seq, type_, payload)

    def _inst(self, name: str) -> InstitutionState:
        st = self._institutions.get(name)
        if st is None:
            st = InstitutionState(name=name)
            self._institutions[name] = st
        return st

    def _save_inst(self, st: InstitutionState) -> None:
        self._store.upsert("institutions", {"name": st.name}, st.to_dict())

    def _save_quote(self, q: Quote) -> None:
        self._store.upsert(
            "quotes",
            {"institution": q.institution, "quote_id": q.quote_id},
            q.to_dict(),
        )

    def _save_intent(self, it: Intent) -> None:
        self._store.upsert(
            "intents",
            {"intent_id": it.intent_id, "idempotency_key": it.idempotency_key},
            it.to_dict(),
        )

    def _save_allocation(self, a: Allocation) -> None:
        self._store.upsert(
            "allocations",
            {"allocation_id": a.allocation_id, "intent_id": a.intent_id},
            a.to_dict(),
        )

    def _save_decision(self, d: Decision) -> None:
        self._store.upsert(
            "decisions",
            {"decision_id": d.decision_id, "intent_id": d.intent_id},
            d.to_dict(),
        )

    def _save_idem_meta(self) -> None:
        self._store.set_meta("seen_snapshots", "\n".join(sorted(self._seen_snapshots)))
        self._store.set_meta("seen_fills", json.dumps(self._seen_fills, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 报价接入
    # ------------------------------------------------------------------

    def ingest_snapshot(
        self,
        institution: str,
        quotes: list[dict],
        snapshot_id: Optional[str] = None,
    ) -> dict:
        """接收某机构的报价快照：快照语义为整体替换该机构的活动报价。

        离线补传也走这里：迟到的快照按到达时刻打 received_at，
        已过有效期的报价只入档审计、永不参与路由；历史决策不受影响。
        snapshot_id 幂等：网络重试重复投递不会产生重复报价。
        """
        with self._lock, self._store.transaction():
            if snapshot_id is not None and snapshot_id in self._seen_snapshots:
                return {"institution": institution, "snapshot_id": snapshot_id,
                        "deduplicated": True, "accepted": [], "rejected": []}

            now = self._clock.wall()
            self._inst(institution)  # 确保机构登记在册

            # 旧活动报价整体替换
            replaced = 0
            for q in self._quotes.values():
                if q.institution == institution and q.status is QuoteStatus.ACTIVE:
                    q.status = QuoteStatus.REPLACED
                    self._save_quote(q)
                    replaced += 1

            accepted, rejected = [], []
            for raw in quotes:
                try:
                    q = self._build_quote(institution, raw, now)
                except (KeyError, ValueError) as exc:
                    rejected.append({"quote": raw, "reason": f"INVALID:{exc}"})
                    continue
                self._quotes[q.key] = q
                self._save_quote(q)
                accepted.append(q.to_dict())

            if snapshot_id is not None:
                self._seen_snapshots.add(snapshot_id)
                self._save_idem_meta()
            self._event(
                "SNAPSHOT_INGESTED",
                {"institution": institution, "snapshot_id": snapshot_id,
                 "accepted": len(accepted), "replaced": replaced,
                 "received_at": fmt_time(now)},
            )
            decisions = self._reevaluate_pending()
            return {
                "institution": institution,
                "snapshot_id": snapshot_id,
                "deduplicated": False,
                "accepted": accepted,
                "rejected": rejected,
                "replaced": replaced,
                "triggered_decisions": decisions,
            }

    def _build_quote(self, institution: str, raw: dict, now: datetime) -> Quote:
        price = to_decimal(raw["price"])
        min_amount = to_decimal(raw["min_amount"])
        max_amount = to_decimal(raw["max_amount"])
        valid_from = parse_time(raw["valid_from"])
        valid_until = parse_time(raw["valid_until"])
        if price <= 0:
            raise ValueError("price 必须为正")
        if min_amount <= 0:
            raise ValueError("min_amount 必须为正")
        if max_amount < min_amount:
            raise ValueError("max_amount 不得小于 min_amount")
        if valid_until <= valid_from:
            raise ValueError("valid_until 必须晚于 valid_from")
        return Quote(
            institution=institution,
            quote_id=str(raw["quote_id"]),
            ccy_pair=str(raw["ccy_pair"]),
            price=price,
            min_amount=min_amount,
            max_amount=max_amount,
            valid_from=valid_from,
            valid_until=valid_until,
            received_at=now,
            received_seq=self._next_seq(),
        )

    def withdraw_quote(self, institution: str, quote_id: str) -> dict:
        """撤回报价：对后续决策立即排除；已锁定的分配不受影响。
        重复撤回返回相同结果（幂等）。"""
        with self._lock, self._store.transaction():
            q = self._quotes.get((institution, quote_id))
            if q is None:
                return {"institution": institution, "quote_id": quote_id,
                        "status": "NOT_FOUND"}
            if q.status is QuoteStatus.ACTIVE:
                q.status = QuoteStatus.WITHDRAWN
                self._save_quote(q)
                self._event("QUOTE_WITHDRAWN",
                            {"institution": institution, "quote_id": quote_id})
            return {"institution": institution, "quote_id": quote_id,
                    "status": q.status.value}

    # ------------------------------------------------------------------
    # 购汇意图
    # ------------------------------------------------------------------

    def submit_intent(
        self,
        idempotency_key: str,
        ccy_pair: str,
        amount: Any,
        intent_id: Optional[str] = None,
    ) -> dict:
        """提交购汇意图并立即尝试路由。

        幂等：相同 idempotency_key 的重复提交（包括并发重试）返回同一个
        意图，不会产生第二份分配——每笔订单只有一个最终分配。
        """
        with self._lock, self._store.transaction():
            existing_id = self._idem_index.get(idempotency_key)
            if existing_id is not None:
                return self._intent_view(self._intents[existing_id], deduplicated=True)

            seq = self._next_seq()
            intent_id = intent_id or f"I-{seq}"
            if intent_id in self._intents:
                raise EngineError("INTENT_ID_CONFLICT", f"intent_id 已存在: {intent_id}")

            now = self._clock.wall()
            intent = Intent(
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                ccy_pair=str(ccy_pair),
                amount=Decimal(0),
                created_at=now,
                created_seq=seq,
            )
            try:
                parsed = to_decimal(amount)
                if parsed <= 0:
                    raise ValueError("amount 必须为正")
                if not str(ccy_pair):
                    raise ValueError("ccy_pair 不能为空")
                intent.amount = parsed
            except ValueError as exc:
                intent.status = IntentStatus.REJECTED
                intent.reject_reason = str(exc)

            self._intents[intent_id] = intent
            self._idem_index[idempotency_key] = intent_id
            self._save_intent(intent)
            self._event("INTENT_SUBMITTED",
                        {"intent_id": intent_id, "idempotency_key": idempotency_key,
                         "status": intent.status.value})

            if intent.status is IntentStatus.REJECTED:
                return self._intent_view(intent, deduplicated=False)

            self._evaluate_intent(intent)
            return self._intent_view(intent, deduplicated=False)

    def _evaluate_intent(self, intent: Intent) -> Optional[Decision]:
        """对单个意图执行一次路由求值并落库决策记录。"""
        remainder = self._uncovered(intent)
        if remainder <= 0 or intent.status in TERMINAL_INTENT_STATES:
            return None

        seq = self._next_seq()
        now = self._clock.wall()
        pair_quotes = [q for q in self._quotes.values() if q.ccy_pair == intent.ccy_pair]
        payload = evaluate(
            pair=intent.ccy_pair,
            amount=remainder,
            quotes=pair_quotes,
            institutions=self._institutions,
            now=now,
            seq=seq,
            mono=self._clock.mono(),
            config=self._config,
        )
        decision = Decision(
            decision_id=f"D-{seq}",
            intent_id=intent.intent_id,
            seq=seq,
            payload=payload,
        )
        self._decisions[decision.decision_id] = decision
        self._save_decision(decision)

        # 命中切片 -> 生成锁定分配并预留额度（同一事务，原子生效）
        for slice_ in payload["chosen"]:
            alloc_seq = self._next_seq()
            alloc = Allocation(
                allocation_id=f"A-{alloc_seq}",
                intent_id=intent.intent_id,
                institution=slice_["institution"],
                quote_id=slice_["quote_id"],
                amount=to_decimal(slice_["amount"]),
                price=to_decimal(slice_["price"]),
                decision_id=decision.decision_id,
                created_seq=alloc_seq,
            )
            inst = self._inst(alloc.institution)
            if inst.limit_total is not None:
                inst.limit_reserved += alloc.amount
                self._save_inst(inst)
            self._allocations[alloc.allocation_id] = alloc
            self._save_allocation(alloc)

        self._event("DECISION_MADE", {
            "decision_id": decision.decision_id,
            "intent_id": intent.intent_id,
            "summary": payload["summary"],
            "allocated_total": payload["allocated_total"],
        })
        self._refresh_intent(intent)
        return decision

    def _reevaluate_pending(self) -> list[str]:
        """补处理未决意图：新报价到达、解冻、移出黑名单、释放额度后触发。
        按提交顺序（FIFO）逐个重评估，保证确定性。"""
        made: list[str] = []
        pending = sorted(
            (it for it in self._intents.values()
             if it.status not in TERMINAL_INTENT_STATES and self._uncovered(it) > 0),
            key=lambda it: it.created_seq,
        )
        for intent in pending:
            decision = self._evaluate_intent(intent)
            if decision is not None:
                made.append(decision.decision_id)
        return made

    def _uncovered(self, intent: Intent) -> Decimal:
        """尚未被锁定也未成交的剩余需求量。"""
        locked = sum(
            (a.remaining for a in self._allocations.values()
             if a.intent_id == intent.intent_id and a.status in ACTIVE_ALLOCATION_STATES),
            Decimal(0),
        )
        return intent.amount - intent.filled - locked

    def _refresh_intent(self, intent: Intent) -> None:
        if intent.status in TERMINAL_INTENT_STATES:
            return
        locked = sum(
            (a.remaining for a in self._allocations.values()
             if a.intent_id == intent.intent_id and a.status in ACTIVE_ALLOCATION_STATES),
            Decimal(0),
        )
        covered = intent.filled + locked
        if intent.filled >= intent.amount:
            intent.status = IntentStatus.FILLED
        elif covered >= intent.amount:
            intent.status = IntentStatus.ALLOCATED
        elif covered > 0:
            intent.status = IntentStatus.PARTIAL
        else:
            intent.status = IntentStatus.PENDING
        intent.version += 1
        self._save_intent(intent)

    # ------------------------------------------------------------------
    # 成交回报 / 释放 / 撤销
    # ------------------------------------------------------------------

    def record_fill(self, allocation_id: str, amount: Any,
                    fill_id: Optional[str] = None) -> dict:
        """登记一笔（部分）成交。fill_id 幂等：网络重试不会重复入账。"""
        with self._lock, self._store.transaction():
            if fill_id is not None and fill_id in self._seen_fills:
                return {**self._seen_fills[fill_id], "deduplicated": True}

            alloc = self._allocations.get(allocation_id)
            if alloc is None:
                raise EngineError("ALLOCATION_NOT_FOUND", f"分配不存在: {allocation_id}")
            if alloc.status not in ACTIVE_ALLOCATION_STATES:
                raise EngineError("ALLOCATION_CLOSED",
                                  f"分配已终结({alloc.status.value})，不能再成交")
            take = to_decimal(amount)
            if take <= 0:
                raise EngineError("INVALID_AMOUNT", "成交金额必须为正")
            if take > alloc.remaining:
                raise EngineError(
                    "FILL_EXCEEDS_LOCK",
                    f"成交 {take} 超过锁定余量 {alloc.remaining}",
                )

            alloc.filled += take
            alloc.status = (AllocationStatus.FILLED if alloc.remaining == 0
                            else AllocationStatus.PARTIALLY_FILLED)
            self._save_allocation(alloc)

            # 额度：预留转核销
            inst = self._inst(alloc.institution)
            if inst.limit_total is not None:
                inst.limit_reserved -= take
                inst.limit_used += take
                self._save_inst(inst)

            intent = self._intents[alloc.intent_id]
            intent.filled += take
            self._refresh_intent(intent)
            self._event("FILL_RECORDED", {
                "allocation_id": allocation_id, "amount": str(take),
                "intent_id": intent.intent_id, "intent_status": intent.status.value,
            })

            # 若意图仍有未覆盖余量，立即尝试补路由
            if self._uncovered(intent) > 0:
                self._evaluate_intent(intent)

            receipt = {
                "allocation_id": allocation_id,
                "filled": str(alloc.filled),
                "allocation_status": alloc.status.value,
                "intent_id": intent.intent_id,
                "intent_status": intent.status.value,
                "deduplicated": False,
            }
            if fill_id is not None:
                self._seen_fills[fill_id] = receipt
                self._save_idem_meta()
            return receipt

    def release_allocation(self, allocation_id: str) -> dict:
        """释放分配的未成交余量（交易对手无法足额成交时），
        释放后意图余量回到待路由并立即补评估。"""
        with self._lock, self._store.transaction():
            alloc = self._allocations.get(allocation_id)
            if alloc is None:
                raise EngineError("ALLOCATION_NOT_FOUND", f"分配不存在: {allocation_id}")
            if alloc.status not in ACTIVE_ALLOCATION_STATES:
                return {"allocation_id": allocation_id, "status": alloc.status.value}

            leftover = alloc.remaining
            alloc.status = (AllocationStatus.FILLED if leftover == 0
                            else AllocationStatus.RELEASED)
            self._save_allocation(alloc)
            inst = self._inst(alloc.institution)
            if inst.limit_total is not None and leftover > 0:
                inst.limit_reserved -= leftover
                self._save_inst(inst)
            self._event("ALLOCATION_RELEASED", {
                "allocation_id": allocation_id, "released": str(leftover),
            })

            intent = self._intents[alloc.intent_id]
            self._refresh_intent(intent)
            if self._uncovered(intent) > 0:
                self._evaluate_intent(intent)
            return {"allocation_id": allocation_id, "status": alloc.status.value,
                    "released": str(leftover)}

    def cancel_intent(self, intent_id: str) -> dict:
        """撤销意图：释放全部未成交锁定，状态进入终态 CANCELLED。"""
        with self._lock, self._store.transaction():
            intent = self._intents.get(intent_id)
            if intent is None:
                raise EngineError("INTENT_NOT_FOUND", f"意图不存在: {intent_id}")
            if intent.status in TERMINAL_INTENT_STATES:
                return self._intent_view(intent, deduplicated=False)
            for alloc in self._allocations.values():
                if (alloc.intent_id == intent_id
                        and alloc.status in ACTIVE_ALLOCATION_STATES):
                    leftover = alloc.remaining
                    alloc.status = (AllocationStatus.FILLED if leftover == 0
                                    else AllocationStatus.RELEASED)
                    self._save_allocation(alloc)
                    inst = self._inst(alloc.institution)
                    if inst.limit_total is not None and leftover > 0:
                        inst.limit_reserved -= leftover
                        self._save_inst(inst)
            intent.status = IntentStatus.CANCELLED
            intent.version += 1
            self._save_intent(intent)
            self._event("INTENT_CANCELLED", {"intent_id": intent_id})
            return self._intent_view(intent, deduplicated=False)

    # ------------------------------------------------------------------
    # 机构管控：冻结 / 黑名单 / 额度
    # ------------------------------------------------------------------

    def freeze(self, institution: str) -> dict:
        """人工冻结：该机构报价立即从后续路由中排除（已锁定分配不受影响）。
        未决意图保持 PENDING，解冻后自动补处理。"""
        with self._lock, self._store.transaction():
            st = self._inst(institution)
            st.frozen = True
            self._save_inst(st)
            self._event("INSTITUTION_FROZEN", {"institution": institution})
            return st.to_dict()

    def unfreeze(self, institution: str) -> dict:
        """解除冻结，并立即补处理积压的未决意图。"""
        with self._lock, self._store.transaction():
            st = self._inst(institution)
            st.frozen = False
            self._save_inst(st)
            self._event("INSTITUTION_UNFROZEN", {"institution": institution})
            decisions = self._reevaluate_pending()
            return {**st.to_dict(), "triggered_decisions": decisions}

    def set_blacklist(self, institution: str, blacklisted: bool) -> dict:
        """合规黑名单：命中即排除；移出黑名单后补处理未决意图。"""
        with self._lock, self._store.transaction():
            st = self._inst(institution)
            st.blacklisted = blacklisted
            self._save_inst(st)
            self._event("BLACKLIST_UPDATED",
                        {"institution": institution, "blacklisted": blacklisted})
            decisions = self._reevaluate_pending() if not blacklisted else []
            return {**st.to_dict(), "triggered_decisions": decisions}

    def set_limit(self, institution: str, limit_total: Optional[Any]) -> dict:
        """设置机构总额度（None 表示不限额）。只影响后续预留。"""
        with self._lock, self._store.transaction():
            st = self._inst(institution)
            st.limit_total = None if limit_total is None else to_decimal(limit_total)
            if st.limit_total is not None and st.limit_total < 0:
                raise EngineError("INVALID_LIMIT", "额度不能为负")
            self._save_inst(st)
            self._event("LIMIT_UPDATED",
                        {"institution": institution,
                         "limit_total": None if st.limit_total is None
                         else str(st.limit_total)})
            return st.to_dict()

    def evaluate_intent(self, intent_id: str) -> dict:
        """手动触发一次重评估（如运维确认网络恢复后）。"""
        with self._lock, self._store.transaction():
            intent = self._intents.get(intent_id)
            if intent is None:
                raise EngineError("INTENT_NOT_FOUND", f"意图不存在: {intent_id}")
            decision = self._evaluate_intent(intent)
            return self._intent_view(
                intent, deduplicated=False,
                last_decision=None if decision is None else decision.decision_id,
            )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _intent_view(self, intent: Intent, deduplicated: bool,
                     last_decision: Optional[str] = None) -> dict:
        allocs = [a.to_dict() for a in self._allocations.values()
                  if a.intent_id == intent.intent_id]
        allocs.sort(key=lambda a: a["created_seq"])
        decisions = [
            d.decision_id
            for d in sorted(
                (d for d in self._decisions.values()
                 if d.intent_id == intent.intent_id),
                key=lambda d: d.seq,
            )
        ]
        view = {
            **intent.to_dict(),
            "uncovered": str(self._uncovered(intent)),
            "allocations": allocs,
            "decisions": decisions,
            "deduplicated": deduplicated,
        }
        if last_decision is not None:
            view["last_decision"] = last_decision
        return view

    def get_intent(self, intent_id: str) -> dict:
        with self._lock:
            intent = self._intents.get(intent_id)
            if intent is None:
                raise EngineError("INTENT_NOT_FOUND", f"意图不存在: {intent_id}")
            return self._intent_view(intent, deduplicated=False)

    def get_decision(self, decision_id: str) -> dict:
        """取回完整决策记录：候选快照、逐条理由、时钟信息——
        这就是事后还原比较依据的全部材料。"""
        with self._lock:
            d = self._decisions.get(decision_id)
            if d is None:
                raise EngineError("DECISION_NOT_FOUND", f"决策不存在: {decision_id}")
            return d.to_dict()

    def get_allocation(self, allocation_id: str) -> dict:
        with self._lock:
            a = self._allocations.get(allocation_id)
            if a is None:
                raise EngineError("ALLOCATION_NOT_FOUND", f"分配不存在: {allocation_id}")
            return a.to_dict()

    def state_dump(self) -> dict:
        with self._lock:
            return {
                "seq": self._seq,
                "quotes": [q.to_dict() for q in self._quotes.values()],
                "institutions": [s.to_dict() for s in self._institutions.values()],
                "intents": [self._intent_view(i, deduplicated=False)
                            for i in self._intents.values()],
                "decisions": [d.decision_id for d in self._decisions.values()],
            }
