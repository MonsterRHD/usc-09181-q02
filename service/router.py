"""报价路由器核心。

线程模型：单个 ``RLock`` 串行化所有决策，保证并发提交相同意图时
只有第一个进入路由，其余以 ``DUPLICATE`` 返回同一结果。

金额口径：``amount`` / ``size`` / ``min_size`` 均为**外币名义金额**；
``rate`` 为每单位外币的人民币价格（报价币种 CNY/CNH），企业购汇
人民币成本 = rate × amount。候选只在同一报价币种间比较。

时钟纪律：所有有效期/生效期判定都用决策时刻 ``clock.now()``；
报价的 ``received_at`` 是路由器收到时刻。effective_from > 当前时钟
的报价属于“收到后才生效”，禁止引用。
"""

import threading
from decimal import Decimal

from .clock import Clock
from .models import Ccy, Intent, IntentStatus, InstitutionState, Quote, QuoteStatus
from .store import Journal

RATE_D = Decimal("0.000001")
AMT_D = Decimal("0.01")


def _d(x) -> Decimal:
    return Decimal(str(x))


def _f(x: Decimal) -> float:
    return float(x)


def _money(x: Decimal) -> Decimal:
    return x.quantize(AMT_D)


class Router:
    def __init__(self, journal: Journal, clock: Clock = None):
        self.journal = journal
        self.clock = clock or Clock()
        self._lock = threading.RLock()
        self.quotes = {}            # quote_id -> Quote
        self.remaining = {}         # quote_id -> Decimal
        self.institutions = {}      # name -> InstitutionState
        self.intents = {}           # idempotency_key -> record(dict)
        self.by_intent_id = {}      # intent_id -> key
        self.pending = []           # FROZEN_QUEUED 的 intent_id（提交顺序）
        self._recover()

    # ---------------------------------------------------------------- 恢复
    def _recover(self):
        for event in self.journal.replay():
            self._apply(event, replay=True)

    def _emit(self, event_type: str, payload: dict, at: float = None):
        at = self.clock.now() if at is None else at
        ev = {"type": event_type, "at": at, "payload": payload}
        self.journal.append(event_type, at, payload)
        self._apply(ev)
        return ev

    def _apply(self, ev: dict, replay: bool = False):
        t = ev["type"]
        at = ev["at"]
        p = ev["payload"]
        if t == "QUOTE_RECEIVED":
            if p["quote_id"] not in self.quotes:  # 重放/重试幂等
                q = Quote.from_snapshot(p)
                self.quotes[q.quote_id] = q
                self.remaining[q.quote_id] = _d(q.size)
        elif t == "QUOTE_WITHDRAWN":
            q = self.quotes.get(p["quote_id"])
            if q and q.withdrawn_at is None:
                q.withdrawn_at = at
        elif t == "INSTITUTION_STATE":
            self.institutions[p["institution"]] = InstitutionState(p["state"])
        elif t == "INTENT_SUBMITTED":
            if p["idempotency_key"] not in self.intents:
                rec = self._new_record(p)
                self.intents[p["idempotency_key"]] = rec
                self.by_intent_id[p["intent_id"]] = p["idempotency_key"]
        elif t == "INTENT_DUPLICATE":
            rec = self.intents.get(p["idempotency_key"])
            if rec is not None:
                self.by_intent_id.setdefault(p["intent_id"], p["idempotency_key"])
                rec["duplicates"].append(
                    {"intent_id": p["intent_id"], "submitted_at": p["submitted_at"], "at": at}
                )
        elif t == "INTENT_QUEUED":
            rec = self._rec_by_intent(p["intent_id"])
            if rec:
                rec["status"] = IntentStatus.FROZEN_QUEUED.value
                if p["intent_id"] not in self.pending:
                    self.pending.append(p["intent_id"])
        elif t == "ATTEMPT_DECIDED":
            rec = self._rec_by_intent(p["intent_id"])
            if rec:
                rec["attempts"].append(
                    {"decided_at": at, "baseline": p["baseline"], "ranking": p["ranking"]}
                )
        elif t == "FILL_RESERVED":
            q = self.quotes[p["quote_id"]]
            amt = _d(p["amount"])
            self.remaining[q.quote_id] = _money(self.remaining[q.quote_id] - amt)
            rec = self._rec_by_intent(p["intent_id"])
            piece = {
                "seq": p["seq"],
                "institution": p["institution"],
                "quote_id": p["quote_id"],
                "rate": p["rate"],
                "amount": p["amount"],
                "filled_at": at,
                "state": "RESERVED",
            }
            rec["pieces"].append(piece)
            rec["filled"] = str(_money(_d(rec["filled"]) + amt))
        elif t == "FILL_CONFIRMED":
            rec = self._rec_by_intent(p["intent_id"])
            for pc in rec["pieces"]:
                if pc["seq"] == p["seq"]:
                    pc["state"] = "FILLED"
        elif t == "FILL_FAILED":
            rec = self._rec_by_intent(p["intent_id"])
            for pc in rec["pieces"]:
                if pc["seq"] == p["seq"]:
                    orig = _d(pc["amount"])
                    effective = _money(_d(p.get("filled_amount", 0)))
                    shortfall = _money(orig - effective)
                    # 未成交部分（含全部失败）释放回报价额度
                    self.remaining[pc["quote_id"]] = _money(
                        self.remaining[pc["quote_id"]] + shortfall
                    )
                    rec["filled"] = str(_money(_d(rec["filled"]) - shortfall))
                    if effective > 0:
                        pc["amount"] = str(effective)
                        pc["state"] = "FILLED"
                    else:
                        pc["state"] = "FAILED"
                        pc["fail_reason"] = p.get("reason")
                        # 完全失败的报价在本意图内隔离，避免改路时反复选同一家
                        if pc["quote_id"] not in rec["failed_quotes"]:
                            rec["failed_quotes"].append(pc["quote_id"])
        elif t == "INTENT_STATUS":
            rec = self._rec_by_intent(p["intent_id"])
            if rec:
                rec["status"] = p["status"]
                if p["status"] != IntentStatus.FROZEN_QUEUED.value:
                    if p["intent_id"] in self.pending:
                        self.pending.remove(p["intent_id"])

    def _new_record(self, p: dict) -> dict:
        return {
            "intent_id": p["intent_id"],
            "idempotency_key": p["idempotency_key"],
            "ccy": p["ccy"],
            "amount": p["amount"],
            "submitted_at": p["submitted_at"],
            "status": IntentStatus.PENDING.value,
            "pieces": [],
            "attempts": [],
            "filled": "0",
            "failed_quotes": [],   # 本意图内已回报失败的报价，不再选用
            "duplicates": list(p.get("duplicates", [])),
        }

    def _rec_by_intent(self, intent_id: str):
        key = self.by_intent_id.get(intent_id)
        return self.intents.get(key) if key else None

    # ------------------------------------------------------------ 报价入口
    def ingest_quote(self, quote: Quote) -> dict:
        with self._lock:
            if quote.quote_id in self.quotes:  # 网络重发：同一报价幂等
                return {"quote_id": quote.quote_id, "deduplicated": True}
            self._emit("QUOTE_RECEIVED", quote.to_snapshot(), at=quote.received_at)
            return {"quote_id": quote.quote_id, "deduplicated": False}

    def withdraw_quote(self, quote_id: str, at: float = None) -> dict:
        with self._lock:
            q = self.quotes.get(quote_id)
            if q is None:
                raise KeyError(f"unknown quote {quote_id}")
            if q.withdrawn_at is None:
                self._emit("QUOTE_WITHDRAWN", {"quote_id": quote_id}, at=at)
            return {"quote_id": quote_id, "withdrawn_at": q.withdrawn_at}

    # ------------------------------------------------------ 机构冻结/黑名单
    def set_frozen(self, institution: str, frozen: bool):
        with self._lock:
            if self.institutions.get(institution) == InstitutionState.BLACKLISTED:
                return  # 合规黑名单优先，冻结不能覆盖
            self._set_state(institution, InstitutionState.FROZEN if frozen else InstitutionState.ACTIVE)
            if not frozen:
                self._drain_pending()

    def set_blacklisted(self, institution: str, blacklisted: bool):
        with self._lock:
            self._set_state(
                institution,
                InstitutionState.BLACKLISTED if blacklisted else InstitutionState.ACTIVE,
            )
            if not blacklisted:
                self._drain_pending()

    def _set_state(self, institution: str, state: InstitutionState):
        if self.institutions.get(institution) != state:
            self._emit(
                "INSTITUTION_STATE",
                {"institution": institution, "state": state.value},
            )

    def _drain_pending(self):
        """恢复/解冻后按提交顺序补处理未决意图。"""
        for intent_id in list(self.pending):
            rec = self._rec_by_intent(intent_id)
            if rec and rec["status"] == IntentStatus.FROZEN_QUEUED.value:
                self._route(rec)

    # ------------------------------------------------------------ 意图入口
    def submit_intent(self, intent: Intent) -> dict:
        with self._lock:
            key = intent.idempotency_key
            if key in self.intents:
                # 同一意图重复提交：记录重复事件，返回唯一既有分配
                self._emit(
                    "INTENT_DUPLICATE",
                    {
                        "idempotency_key": key,
                        "intent_id": intent.intent_id,
                        "submitted_at": intent.submitted_at,
                    },
                )
                rec = self.intents[key]
                return self._view(rec, duplicate_of=rec["intent_id"])

            self._emit("INTENT_SUBMITTED", intent.to_snapshot(), at=intent.submitted_at)
            rec = self.intents[key]
            self._route(rec)
            return self._view(rec)

    # ------------------------------------------------------------ 核心路由
    def _classify(self, q: Quote, need: Decimal, ccy: Ccy, now: float,
                  failed_quotes=()):
        if q.quote_id in set(failed_quotes):
            return False, QuoteStatus.FILL_REJECTED.value
        inst_state = self.institutions.get(q.institution, InstitutionState.ACTIVE)
        if inst_state == InstitutionState.BLACKLISTED:
            return False, QuoteStatus.BLACKLISTED.value
        if q.ccy != ccy:
            return False, QuoteStatus.CCY_MISMATCH.value
        if inst_state == InstitutionState.FROZEN:
            return False, QuoteStatus.FROZEN.value
        if q.withdrawn_at is not None and q.withdrawn_at <= now:
            return False, QuoteStatus.WITHDRAWN.value
        if now >= q.valid_until:
            return False, QuoteStatus.EXPIRED.value
        # 硬性合规：生效时间晚于“收到时间”的报价永远不得被任何成交引用；
        # 同时决策时刻尚未生效的报价也不可选。
        if q.effective_from > q.received_at or q.effective_from > now:
            return False, QuoteStatus.NOT_YET_EFFECTIVE.value
        rem = self.remaining[q.quote_id]
        if rem < _d(q.min_size):
            return False, QuoteStatus.INSUFFICIENT_LIMIT.value
        if need < _d(q.min_size):
            return False, QuoteStatus.BELOW_MIN_SIZE.value
        return True, None

    def _baseline(self, rec: dict, need: Decimal, now: float):
        ccy = Ccy(rec["ccy"])
        rows, eligible = [], []
        fq = set(rec.get("failed_quotes", ()))
        for q in self.quotes.values():
            ok, reason = self._classify(q, need, ccy, now, failed_quotes=fq)
            row = {
                **q.to_snapshot(),
                "remaining": _f(self.remaining[q.quote_id]),
                "age_seconds": round(now - q.received_at, 6),
                "eligible": ok,
                "reason": reason,
                "cost": round(_f(_d(q.rate) * need), 2) if ok or reason == QuoteStatus.BELOW_MIN_SIZE.value else None,
            }
            rows.append(row)
            if ok:
                eligible.append(q)
        # 确定性排序：价格优先（人民币成本低），再比新鲜度，再机构名/报价号
        eligible.sort(
            key=lambda q: (
                _d(q.rate),
                Decimal(str(now - q.received_at)),
                q.institution,
                q.quote_id,
            )
        )
        order = {qid: i for i, q in enumerate(eligible) for qid in [q.quote_id]}
        for row in rows:
            row["rank"] = order.get(row["quote_id"])
        ranking = [q.quote_id for q in eligible]
        # 原始顺序固定（按报价号），便于逐次比较结果可复现
        rows.sort(key=lambda r: r["quote_id"])
        return rows, ranking, eligible

    def _route(self, rec: dict):
        now = self.clock.now()
        total = _d(rec["amount"])
        filled = _d(rec["filled"])
        need = _money(total - filled)

        baseline, ranking, eligible = self._baseline(rec, need if need > 0 else total, now)
        frozen_present = any(
            r["reason"] == QuoteStatus.FROZEN.value and r["ccy"] == rec["ccy"]
            for r in baseline
        )

        if need <= 0:
            self._finish(rec)
            return

        self._emit(
            "ATTEMPT_DECIDED",
            {
                "intent_id": rec["intent_id"],
                "baseline": {"clock": now, "candidates": baseline},
                "ranking": ranking,
            },
            at=now,
        )

        if not eligible:
            if frozen_present:
                # 即使已部分成交，剩余缺口也挂起，解冻后补处理
                self._emit(
                    "INTENT_QUEUED",
                    {"intent_id": rec["intent_id"], "reason": QuoteStatus.FROZEN.value},
                    at=now,
                )
                return
            status = (
                IntentStatus.PARTIALLY_FILLED.value
                if filled > 0
                else IntentStatus.DECLINED.value
            )
            self._emit("INTENT_STATUS", {"intent_id": rec["intent_id"], "status": status}, at=now)
            return

        # 按候选顺序预留：每家取 min(剩余额度, 未成交量)，锁定该报价
        seq_start = 1 + max([p["seq"] for p in rec["pieces"]], default=0)
        seq = seq_start
        for q in eligible:
            if need <= 0:
                break
            take = _money(min(self.remaining[q.quote_id], need))
            if take < _d(q.min_size):
                continue
            self._emit(
                "FILL_RESERVED",
                {
                    "intent_id": rec["intent_id"],
                    "seq": seq,
                    "institution": q.institution,
                    "quote_id": q.quote_id,
                    "rate": q.rate,
                    "amount": str(take),
                    "remaining_after": str(_money(self.remaining[q.quote_id] - take)),
                },
                at=now,
            )
            need = _money(need - take)
            seq += 1

        self._finish(rec, frozen_present=frozen_present, clock=now)

    def _finish(self, rec: dict, frozen_present: bool = False, clock: float = None):
        filled = _d(rec["filled"])
        total = _d(rec["amount"])
        if filled >= total:
            status = IntentStatus.FILLED.value
        elif filled > 0:
            status = (
                IntentStatus.FROZEN_QUEUED.value
                if frozen_present
                else IntentStatus.PARTIALLY_FILLED.value
            )
        else:
            status = IntentStatus.FAILED.value
        ev = {"intent_id": rec["intent_id"], "status": status}
        self._emit("INTENT_STATUS", ev, at=clock)
        if status == IntentStatus.FROZEN_QUEUED.value and rec["intent_id"] not in self.pending:
            # 部分成交后因冻结排队等待补处理
            self._emit("INTENT_QUEUED", {"intent_id": rec["intent_id"]}, at=clock)

    # -------------------------------------------------- 成交确认/失败/短量
    def confirm_fill(self, intent_id: str, seq: int):
        """模拟交易对手正常成交确认（网络延迟后到达）。"""
        with self._lock:
            rec = self._rec_by_intent(intent_id)
            if rec is None:
                raise KeyError(intent_id)
            if any(p["seq"] == seq and p["state"] == "RESERVED" for p in rec["pieces"]):
                self._emit("FILL_CONFIRMED", {"intent_id": intent_id, "seq": seq})
            return self._view(rec)

    def report_fill_result(
        self, intent_id: str, seq: int, success: bool, filled_amount: str = None,
        reason: str = None,
    ):
        """成交回报：失败或部分短量。释放预留后立即按当前时钟补路由。

        仍然只有同一个 Allocation（intent 记录），新增一次带时钟与
        比较依据的 attempt，保证失败改路可审计、不产生第二个最终分配。
        """
        with self._lock:
            rec = self._rec_by_intent(intent_id)
            if rec is None:
                raise KeyError(intent_id)
            piece = next((p for p in rec["pieces"] if p["seq"] == seq), None)
            if piece is None or piece["state"] != "RESERVED":
                return self._view(rec)
            payload = {"intent_id": intent_id, "seq": seq, "reason": reason}
            if success and filled_amount is not None:
                fa = _money(_d(filled_amount))
                if fa >= _d(piece["amount"]):
                    self._emit("FILL_CONFIRMED", {"intent_id": intent_id, "seq": seq})
                    return self._view(rec)
                payload["filled_amount"] = str(fa)
            elif success:
                self._emit("FILL_CONFIRMED", {"intent_id": intent_id, "seq": seq})
                return self._view(rec)
            self._emit("FILL_FAILED", payload)
            # 在同一意图上补路由剩余缺口
            self._route(rec)
            return self._view(rec)

    # ------------------------------------------------------------ 查询视图
    def _view(self, rec: dict, duplicate_of: str = None) -> dict:
        attempts = []
        for a in rec["attempts"]:
            attempts.append(
                {
                    "decided_at": a["decided_at"],
                    "ranking": a["ranking"],
                    "baseline": a["baseline"],
                }
            )
        view = {
            "intent_id": rec["intent_id"],
            "idempotency_key": rec["idempotency_key"],
            "ccy": rec["ccy"],
            "amount": rec["amount"],
            "status": rec["status"],
            "filled": rec["filled"],
            "pieces": [dict(p) for p in rec["pieces"] if p["state"] != "FAILED"],
            "failed_pieces": [dict(p) for p in rec["pieces"] if p["state"] == "FAILED"],
            "attempts": attempts,
            "duplicates": list(rec["duplicates"]),
        }
        if duplicate_of:
            view["duplicate"] = True
            view["duplicate_of"] = duplicate_of
        return view

    def get_intent(self, intent_id: str = None, idempotency_key: str = None):
        with self._lock:
            if intent_id is not None:
                rec = self._rec_by_intent(intent_id)
                if rec and rec["intent_id"] != intent_id:
                    return self._view(rec, duplicate_of=rec["intent_id"])
            else:
                rec = self.intents.get(idempotency_key)
            return self._view(rec) if rec else None

    def pending_intents(self):
        with self._lock:
            return list(self.pending)
