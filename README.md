# 在岸离岸报价路由器

面向进口设备采购的资金管理场景：接收多家机构的在岸/离岸人民币报价快照与
企业购汇意图，按**可用额度、最小成交量、报价新鲜度、合规黑名单**给出
可解释的候选顺序并锁定价格。纯 Python 3.11 标准库实现，无第三方依赖。

## 运行

```bash
PORT=8000 DB_PATH=./router.db python3 -m service.main   # 或安装后运行 service
python3 -m unittest discover -s tests                   # 运行测试
```

## 设计要点

- **分层**：`router.py` 是纯函数路由核心（无 I/O、不读时钟，同输入必同输出）；
  `engine.py` 用单锁串行化所有命令并写穿 SQLite；`store.py` 负责持久化与
  只增不删的事件审计日志；`main.py` 是 HTTP 薄壳。
- **可解释**：每次路由生成决策记录，保存全部候选报价快照、逐条选用/排除
  理由（`FROZEN` / `BLACKLISTED` / `EXPIRED` / `STALE` / `NOT_YET_EFFECTIVE` …）
  与时钟信息（墙钟/单调钟/逻辑序号），`GET /decisions/{id}` 即可还原当时的
  比较依据。
- **有效期半开区间**：`valid_from <= now < valid_until`。"收到后才生效"的
  报价在生效前一律排除，任何成交都不会引用它；边界行为固定并写入决策记录。
- **幂等**：意图按 `idempotency_key`、快照按 `snapshot_id`、成交按 `fill_id`
  去重——网络重试与重复提交都有确定结果，每笔订单只有一个最终分配。
- **额度账本**：机构额度三段式 `used + reserved <= limit_total`，锁定即预留、
  成交转核销、释放/撤销回滚，并发提交相同金额也不会超卖。
- **冻结补处理**：人工冻结机构即排除其报价（已锁定分配不受影响）；解冻、
  新快照（含离线补传）、移出黑名单都会自动按提交顺序补处理未决意图。
- **重启不丢锁价**：所有变更事务化写穿 SQLite，进程重启后已锁定的价格、
  未决意图、额度账本、幂等键完整恢复。
- **离线补传不改写历史**：迟到的快照按到达时刻打接收时间，已过有效期的
  报价只入档审计、永不参与路由；历史决策记录不可变。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/quotes/snapshot` | 机构报价快照（整体替换，`snapshot_id` 幂等，支持离线补传） |
| POST | `/quotes/withdraw` | 撤回报价（幂等，不影响已锁定分配） |
| POST | `/intents` | 提交购汇意图（`idempotency_key` 幂等） |
| GET | `/intents/{id}` | 意图状态、分配与决策列表 |
| POST | `/intents/{id}/cancel` | 撤销意图，释放全部未成交锁定 |
| POST | `/intents/{id}/evaluate` | 手动触发重评估 |
| POST | `/fills` | 登记（部分）成交（`fill_id` 幂等，超额拒绝） |
| POST | `/allocations/{id}/release` | 释放分配未成交余量并自动补路由 |
| GET | `/allocations/{id}` | 查询分配 |
| POST | `/institutions/{name}/freeze` `/unfreeze` | 人工冻结 / 解冻并补处理 |
| POST | `/institutions/{name}/limit` | 设置机构额度 |
| POST | `/blacklist` | 合规黑名单增删 `{institution, blacklisted}` |
| GET | `/decisions/{id}` | 决策记录（候选快照/理由/时钟） |
| GET | `/state` | 全量状态（审计调试用） |

### 示例

```bash
# 机构上报快照
curl -X POST localhost:8000/quotes/snapshot -d '{
  "institution": "HSBC", "snapshot_id": "S1",
  "quotes": [{"quote_id": "Q1", "ccy_pair": "USD/CNH", "price": "7.1025",
              "min_amount": "10000", "max_amount": "500000",
              "valid_from": "2026-09-18T09:00:00Z",
              "valid_until": "2026-09-18T23:00:00Z"}]}'

# 企业提交购汇意图（重复提交同一 key 返回同一结果）
curl -X POST localhost:8000/intents -d '{
  "idempotency_key": "CORP-001", "ccy_pair": "USD/CNH", "amount": "200000"}'
```

## 状态机

- **意图**：`PENDING`（待路由）→ `ALLOCATED`（已足额锁定/覆盖）→ `FILLED`；
  覆盖不足为 `PARTIAL`；`CANCELLED` / `REJECTED` 为终态。
- **分配**：`LOCKED` → `PARTIALLY_FILLED` → `FILLED`；未成交余量可 `RELEASED`。
- **报价**：`ACTIVE` → `WITHDRAWN` / `REPLACED`；状态迁移只影响后续决策。

金额与价格内部一律使用 `Decimal`，JSON 中以字符串传输，避免浮点误差。
敏感配置请放在本地环境文件中（`PORT`、`DB_PATH`）。
