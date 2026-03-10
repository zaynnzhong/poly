# Polymarket 政治选举套利系统 — 设计文档

> 日期: 2026-03-10
> 状态: Approved

---

## 1. 概述

构建一个自动运行的 Polymarket 政治选举套利监控与模拟交易系统。系统通过 WebSocket 实时接收订单簿数据，利用逻辑约束引擎发现定价偏差，通过模拟执行器验证策略，并提供 Web 面板供人工审核和监控。

### 范围

- **聚焦市场：** 仅政治选举
- **阶段目标：** 模拟交易系统（监控 + 虚拟资金模拟下单）
- **运行形态：** 本地 Python 进程 + Web 面板，后续迁移云端
- **风控参数：** 手册默认值，根据模拟结果调整

### 约束

- 用户当前无 Polymarket 账户和 API 凭证
- LLM 解析市场关系需人工审核后生效
- 支持 Claude API + Gemini API 双 LLM 可切换

---

## 2. 架构：WebSocket 实时 + 多线程

```
┌──────────────────────────────────────────────────────┐
│                    主进程 (main.py)                    │
│                                                       │
│  Thread-1: DataStream          Thread-2: Engine       │
│  ┌─────────────────────┐      ┌────────────────────┐  │
│  │ WebSocket CLOB 连接  │─tick→│ 约束校验器          │  │
│  │ 本地订单簿维护       │      │ VWAP 计算          │  │
│  │ (已有 stream.py)    │      │ Frank-Wolfe 求解    │  │
│  └─────────────────────┘      │ Kelly 仓位计算      │  │
│                               │ → 发现机会 → 模拟执行│  │
│                               └────────┬───────────┘  │
│                                        │写入           │
│                                   ┌────▼────┐         │
│                                   │ SQLite  │         │
│                                   └────┬────┘         │
│                                        │读取           │
│  Thread-3: Web (FastAPI + Uvicorn)     │              │
│  ┌─────────────────────────────────────┘              │
│  │ REST API + 静态 HTML/JS 面板                       │
│  └────────────────────────────────────────────────────│
└──────────────────────────────────────────────────────┘
```

**线程间通信：**
- DataStream → Engine：订单簿更新时通过回调触发，`threading.Lock` 保护共享订单簿
- Engine → SQLite：发现机会/执行模拟交易后写入数据库
- Web → SQLite：只读查询，展示状态

---

## 3. 数据层

### 3.1 市场发现

```
启动时:
  REST API (GET /markets) → 拉取所有政治类市场列表
  → 过滤活跃市场 (active=true, 有交易量)
  → 存入 SQLite markets 表
  → 对每个市场的 token_id 建立 WebSocket 订阅

运行中:
  每 5 分钟重新拉取市场列表 → 发现新市场自动订阅
```

### 3.2 数据库 Schema

```sql
-- 监控的市场
markets (
  token_id TEXT PRIMARY KEY,
  slug TEXT,
  question TEXT,
  category TEXT,
  active BOOLEAN,
  last_price REAL,
  last_updated TIMESTAMP
)

-- LLM 解析的逻辑约束 (需人工审核)
relations (
  id INTEGER PRIMARY KEY,
  market_a TEXT REFERENCES markets,
  market_b TEXT REFERENCES markets,
  relation_type TEXT,     -- "subset" / "mutex" / "equivalent"
  description TEXT,
  status TEXT DEFAULT 'pending',  -- pending → approved / rejected
  created_at TIMESTAMP,
  source TEXT             -- "llm_auto" / "manual"
)

-- 约束违反记录 (套利信号)
violations (
  id INTEGER PRIMARY KEY,
  relation_id INTEGER REFERENCES relations,
  price_a REAL,
  price_b REAL,
  violation_amount REAL,
  signal TEXT,
  detected_at TIMESTAMP
)

-- 模拟交易记录
sim_trades (
  id INTEGER PRIMARY KEY,
  violation_id INTEGER REFERENCES violations,
  leg1_token TEXT,
  leg1_side TEXT,
  leg1_price REAL,
  leg1_size REAL,
  leg2_token TEXT,
  leg2_side TEXT,
  leg2_price REAL,
  leg2_size REAL,
  expected_profit REAL,
  status TEXT,            -- "executed" / "aborted" / "partial"
  created_at TIMESTAMP
)

-- 订单簿全量快照 (每次 WebSocket 推送都写入, 按天分片)
orderbook_snapshots (
  id INTEGER PRIMARY KEY,
  token_id TEXT,
  side TEXT,              -- "bids" / "asks"
  price REAL,
  size REAL,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

-- 价格时序
price_ticks (
  id INTEGER PRIMARY KEY,
  token_id TEXT,
  best_bid REAL,
  best_ask REAL,
  mid_price REAL,
  spread REAL,
  bid_depth REAL,
  ask_depth REAL,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

-- 约束校验日志 (每次校验都记录)
check_logs (
  id INTEGER PRIMARY KEY,
  relation_id INTEGER,
  price_a REAL,
  price_b REAL,
  violated BOOLEAN,
  violation_amount REAL,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### 3.3 高频写入策略

- SQLite WAL 模式 + 批量 INSERT（每 100 条或每 200ms 刷一次）
- `orderbook_snapshots` 按天分片：`snapshots_2026_03_10.db`
- 超过 7 天的快照归档压缩
- `price_ticks` 和 `check_logs` 体量小，留在主库

**存储量预估（20 个市场）：**
- `orderbook_snapshots`: ~500 万行/天
- `price_ticks`: ~15 万行/天
- `check_logs`: ~10 万行/天

---

## 4. Engine 核心引擎

```
WebSocket 推送到达
       │
       ▼
┌─ 更新本地订单簿 (lock) ─┐
│  写入 orderbook_snapshots │
│  写入 price_ticks         │
└───────────┬───────────────┘
            │
            ▼
┌─ 约束校验循环 ────────────┐
│  遍历所有 approved 约束    │
│  用最新价格跑 check_violations │
│  写入 check_logs           │
│  无违反 → 等待下次推送     │
│  有违反 → ▼                │
└───────────┬───────────────┘
            │
            ▼
┌─ VWAP 真实成本计算 ───────┐
│  对两腿分别计算 VWAP       │
│  重算实际利润              │
│  利润 < $0.05 → 丢弃      │
└───────────┬───────────────┘
            │
            ▼
┌─ Kelly 仓位计算 ──────────┐
│  estimate_execution_prob   │
│  modified_kelly_fraction   │
│  min(kelly, 深度50%)       │
│  → 确定模拟下单量          │
└───────────┬───────────────┘
            │
            ▼
┌─ 模拟执行器 ──────────────┐
│  Leg1: 记录成交 (用当前VWAP)│
│  等待 500ms                │
│  重新拉取 Leg2 最新价格     │
│  利润仍在 → Leg2 记录成交   │
│  利润消失 → 标记 aborted    │
│  → 写入 sim_trades         │
│  → 写入 violations         │
└───────────────────────────┘
```

**模拟 vs 真实执行：**
- 不调 API 下单，按当前 VWAP 价格"假设成交"
- 保留 500ms 延迟 + Leg2 价格重检，模拟非原子执行风险

---

## 5. LLM 约束解析 + 人工审核

```
新市场出现 → 按 slug/category 分组配对
  → 调 Claude/Gemini API 解析逻辑关系
  → 写入 relations 表 (status='pending')
  → Web 面板审核: [Approve] [Reject] [Edit]
  → approved 后进入引擎校验循环
```

**LLM 配置：**
- 支持 Claude API 和 Gemini API，通过 config.yaml 切换
- 按组配对避免 N² 爆炸（同一选举下的市场两两配对）

---

## 6. Web 面板

**技术栈：** FastAPI + 静态 HTML/JS（无前端框架）

**页面：**
- **Dashboard** — 实时统计卡片 + 最近信号 + 模拟盈亏曲线
- **Markets** — 监控的市场列表 + 实时价格
- **Relations** — 约束审核页（pending/approved/rejected）
- **Signals** — 约束违反/套利信号历史
- **Trades** — 模拟交易记录 + 筛选
- **Settings** — 运行参数查看

**API：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/status | 系统状态 |
| GET | /api/markets | 市场列表 |
| GET | /api/orderbook/:id | 指定市场订单簿 |
| GET | /api/relations | 约束列表 |
| POST | /api/relations/:id | 审核约束 |
| GET | /api/violations | 套利信号列表 |
| GET | /api/trades | 模拟交易记录 |
| GET | /api/stats | 统计数据 |
| GET | /api/price-history/:id | 价格时序 |

**数据刷新：** `setInterval` + `fetch` 轮询 API，每 2 秒

---

## 7. 配置

```yaml
polymarket:
  ws_endpoint: "wss://ws-subscriptions-clob.polymarket.com/ws/market"
  rest_endpoint: "https://clob.polymarket.com"
  category_filter: "politics"
  market_refresh_interval: 300

llm:
  provider: "claude"  # "claude" | "gemini"
  claude_api_key: ""
  gemini_api_key: ""
  auto_pair_on_new_market: true

engine:
  min_profit_threshold: 0.05
  kelly_max_depth_ratio: 0.5
  sim_initial_balance: 10000
  leg2_recheck_delay: 0.5

storage:
  main_db: "data/main.db"
  snapshots_dir: "data/snapshots/"
  archive_after_days: 7

web:
  host: "0.0.0.0"
  port: 8080
```

---

## 8. 云端迁移路径

1. 加 `Dockerfile` + `docker-compose.yaml`
2. 敏感字段改为环境变量 `${CLAUDE_API_KEY}`
3. SQLite volume 持久化
4. 可选：Nginx 反代 + HTTPS

---

## 9. 需要用户提供

| 项目 | 说明 |
|------|------|
| Polymarket 账户 | 注册并获取 CLOB API Key + 私钥（模拟阶段只读 API 可能不需要） |
| Claude API Key | LLM 解析市场逻辑关系 |
| Gemini API Key | 备选 LLM |
| 运行环境 | Python 3.11+，当前 Mac 即可 |

## 10. 待澄清问题

| # | 问题 | 影响 |
|---|------|------|
| 1 | Polymarket 只读 API 是否需要认证？ | 决定能否零凭证启动 |
| 2 | LLM 市场配对的触发频率？新市场出现时一次，还是定期重跑？ | API 用量和成本 |
| 3 | 模拟交易的胜负结算：手动标记还是自动拉取结算状态？ | 是否需要结算模块 |
| 4 | 告警方式：是否需要 Telegram/邮件/Bark 推送？ | 可能新增通知模块 |
| 5 | 是否需要结构化日志（JSON 格式）？ | 日志实现方式 |
