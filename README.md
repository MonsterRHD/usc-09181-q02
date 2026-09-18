# 在岸 / 离岸人民币报价路由器

接收多家机构的 CNY / CNH 报价快照与企业购汇意图，按**可用额度、最小成交量、
报价新鲜度、合规黑名单**给出确定性的候选顺序与可解释的比较依据，并记录每笔
意图的唯一最终分配。

## 运行

```bash
python3 -m service.main          # 默认 0.0.0.0:8000
PORT=8100 JOURNAL_PATH=data/r.journal python3 -m service.main
python3 -m unittest discover -s tests
```

状态全部保存在 `JOURNAL_PATH`（默认 `data/router.journal`）的仅追加事件日志中，
每条事件先 `fsync` 再进入内存；服务重启重放同一日志，已锁定的价格与额度不丢失。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/quotes` | 上报报价快照（时间字段可携带，支持离线补传） |
| POST | `/quotes/{id}/withdraw` | 撤回报价（幂等） |
| POST | `/institutions/{name}/freeze` | 人工冻结/解冻 `{"frozen": true}` |
| POST | `/institutions/{name}/blacklist` | 合规黑名单增删 `{"blacklisted": true}` |
| POST | `/intents` | 提交购汇意图（`idempotency_key` 去重） |
| GET  | `/intents/{id}` | 查询分配、分片与历次决策基线 |
| POST | `/intents/{id}/fills/{seq}/confirm` | 网络延迟后的成交确认 |
| POST | `/intents/{id}/fills/{seq}/result` | 成交回报（失败 / 短量），自动释放额度并改路 |

## 关键语义

- **候选排序（确定性）**：人民币成本（汇率×金额）→ 报价新鲜度（决策时钟 −
  收到时钟，越小越优先）→ 机构名 → 报价号。与报价上报顺序无关。
- **不可选原因**：`BLACKLISTED / FROZEN / WITHDRAWN / EXPIRED /
  NOT_YET_EFFECTIVE / INSUFFICIENT_LIMIT / BELOW_MIN_SIZE / CCY_MISMATCH /
  FILL_REJECTED`，随每次决策的基线快照一起保存。
- **时钟纪律**：有效期左闭右开（`now >= valid_until` 即过期）；
  `effective_from` 晚于 `received_at` 的“收到后才生效”报价在任何时刻都不得引用。
- **冻结排队**：候选全部被冻结时意图进入 `FROZEN_QUEUED`（含部分成交后的剩余
  缺口），解冻后按提交顺序补处理，并追加一份带当时时钟的比较基线。
- **幂等**：同一 `idempotency_key` 的并发/重复提交只产生一次路由、一次额度预留，
  重复方返回 `duplicate_of` 指向唯一既有分配；报价重发按 `quote_id` 去重。
- **成交状态**：`RESERVED → FILLED`（确认）或失败/短量；失败报价在本意图内隔离，
  未成交额度释放后立即在同一意图上改路，全程只有一个 Allocation，历次 attempt
  均保留原始报价、选用理由与时钟信息。
