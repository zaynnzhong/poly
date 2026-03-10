# Polymarket 政治选举套利系统 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an auto-running Polymarket political election arbitrage system with real-time monitoring, simulated trading, and a web dashboard.

**Architecture:** 3-thread model (WebSocket DataStream + Engine + FastAPI Web). SQLite WAL mode with daily snapshot partitioning for high-frequency data. Existing skill modules (data_stream, logic_engine, optimization, executor) are reused as-is and wired together by a new orchestration layer.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, websocket-client, anthropic SDK, google-genai SDK, vanilla HTML/JS

**Spec:** `docs/superpowers/specs/2026-03-10-polymarket-arbitrage-system-design.md`

---

## Chunk 1: Foundation (Config, DB, Git Init)

### Task 1: Initialize Git repo and Python environment

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`

- [ ] **Step 1: Init git repo**

```bash
cd /Users/jackyz/Documents/poly
git init
```

- [ ] **Step 2: Create .gitignore**

```gitignore
__pycache__/
*.pyc
*.pyo
.env
data/
*.db
.venv/
.idea/
.vscode/
```

- [ ] **Step 3: Create pyproject.toml with dependencies**

```toml
[project]
name = "polymarket-arbitrage"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "websocket-client>=1.6.0",
    "requests>=2.31.0",
    "numpy>=1.24.0",
    "fastapi>=0.110.0",
    "uvicorn>=0.27.0",
    "pyyaml>=6.0",
    "anthropic>=0.40.0",
    "google-genai>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-cov"]
```

- [ ] **Step 4: Create default config.yaml**

Create: `config.yaml`

```yaml
polymarket:
  ws_endpoint: "wss://ws-subscriptions-clob.polymarket.com/ws/market"
  rest_endpoint: "https://clob.polymarket.com"
  category_filter: "politics"
  market_refresh_interval: 300

llm:
  provider: "claude"
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
  host: "127.0.0.1"
  port: 8080
```

- [ ] **Step 5: Create config loader**

Create: `config.py`

```python
from __future__ import annotations
import os
from pathlib import Path
import yaml

_DEFAULT_PATH = Path(__file__).parent / "config.yaml"

def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else _DEFAULT_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # env var overrides for secrets
    cfg["llm"]["claude_api_key"] = os.getenv("CLAUDE_API_KEY", cfg["llm"]["claude_api_key"])
    cfg["llm"]["gemini_api_key"] = os.getenv("GEMINI_API_KEY", cfg["llm"]["gemini_api_key"])
    return cfg
```

- [ ] **Step 6: Write test for config loader**

Create: `tests/__init__.py` (empty)
Create: `tests/test_config.py`

```python
from config import load_config

def test_load_default_config():
    cfg = load_config()
    assert cfg["polymarket"]["ws_endpoint"].startswith("wss://")
    assert cfg["engine"]["min_profit_threshold"] == 0.05
    assert cfg["web"]["port"] == 8080
```

- [ ] **Step 7: Run test**

```bash
pytest tests/test_config.py -v
```

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add .gitignore pyproject.toml config.yaml config.py tests/
git commit -m "chore: init project with config loader and dependencies"
```

---

### Task 2: Database layer

**Files:**
- Create: `db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing test for DB schema creation**

```python
# tests/test_db.py
import os, tempfile
from db import Database

def test_create_tables():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {row[0] for row in tables}
        assert "markets" in names
        assert "relations" in names
        assert "violations" in names
        assert "sim_trades" in names
        assert "price_ticks" in names
        assert "check_logs" in names
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_db.py::test_create_tables -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Implement Database class**

Create: `db.py`

```python
"""
SQLite 数据层

- WAL 模式，支持并发读
- 主库: markets, relations, violations, sim_trades, price_ticks, check_logs
- 快照库: orderbook_snapshots 按天分片
- 批量写入缓冲
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    token_id TEXT PRIMARY KEY,
    slug TEXT,
    question TEXT,
    category TEXT,
    active INTEGER DEFAULT 1,
    last_price REAL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_a TEXT REFERENCES markets(token_id),
    market_b TEXT REFERENCES markets(token_id),
    relation_type TEXT,
    description TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    source TEXT
);

CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER REFERENCES relations(id),
    price_a REAL,
    price_b REAL,
    violation_amount REAL,
    signal TEXT,
    detected_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    violation_id INTEGER REFERENCES violations(id),
    leg1_token TEXT,
    leg1_side TEXT,
    leg1_price REAL,
    leg1_size REAL,
    leg2_token TEXT,
    leg2_side TEXT,
    leg2_price REAL,
    leg2_size REAL,
    expected_profit REAL,
    status TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT,
    best_bid REAL,
    best_ask REAL,
    mid_price REAL,
    spread REAL,
    bid_depth REAL,
    ask_depth REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS check_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER,
    price_a REAL,
    price_b REAL,
    violated INTEGER,
    violation_amount REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    timestamp TEXT DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, main_db: str = "data/main.db", snapshots_dir: str = "data/snapshots/"):
        self.main_db_path = main_db
        self.snapshots_dir = Path(snapshots_dir)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # 批量写入缓冲
        self._tick_buffer: list[tuple] = []
        self._snapshot_buffer: list[tuple] = []
        self._check_buffer: list[tuple] = []
        self._buffer_lock = threading.Lock()
        self._last_flush = time.monotonic()

    def init(self) -> None:
        Path(self.main_db_path).parent.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.main_db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, params)
            self._conn.commit()

    # ── 批量缓冲写入 ──

    def buffer_tick(self, token_id: str, best_bid: float, best_ask: float,
                    mid: float, spread: float, bid_depth: float, ask_depth: float) -> None:
        with self._buffer_lock:
            self._tick_buffer.append((
                token_id, best_bid, best_ask, mid, spread, bid_depth, ask_depth,
                datetime.now(timezone.utc).isoformat()
            ))
            self._maybe_flush()

    def buffer_snapshot(self, token_id: str, side: str, price: float, size: float) -> None:
        with self._buffer_lock:
            self._snapshot_buffer.append((
                token_id, side, price, size, datetime.now(timezone.utc).isoformat()
            ))
            self._maybe_flush()

    def buffer_check(self, relation_id: int, price_a: float, price_b: float,
                     violated: bool, amount: float) -> None:
        with self._buffer_lock:
            self._check_buffer.append((
                relation_id, price_a, price_b, int(violated), amount,
                datetime.now(timezone.utc).isoformat()
            ))
            self._maybe_flush()

    def _maybe_flush(self) -> None:
        total = len(self._tick_buffer) + len(self._snapshot_buffer) + len(self._check_buffer)
        elapsed = time.monotonic() - self._last_flush
        if total >= 100 or elapsed >= 0.2:
            self.flush()

    def flush(self) -> None:
        with self._buffer_lock:
            ticks = self._tick_buffer[:]
            snapshots = self._snapshot_buffer[:]
            checks = self._check_buffer[:]
            self._tick_buffer.clear()
            self._snapshot_buffer.clear()
            self._check_buffer.clear()
            self._last_flush = time.monotonic()

        if ticks:
            self.executemany(
                "INSERT INTO price_ticks (token_id,best_bid,best_ask,mid_price,spread,bid_depth,ask_depth,timestamp) VALUES (?,?,?,?,?,?,?,?)",
                ticks,
            )
        if checks:
            self.executemany(
                "INSERT INTO check_logs (relation_id,price_a,price_b,violated,violation_amount,timestamp) VALUES (?,?,?,?,?,?)",
                checks,
            )
        if snapshots:
            self._write_snapshots(snapshots)

    def _write_snapshots(self, rows: list[tuple]) -> None:
        today = date.today().isoformat().replace("-", "_")
        db_path = self.snapshots_dir / f"snapshots_{today}.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SNAPSHOT_SCHEMA)
        conn.executemany(
            "INSERT INTO orderbook_snapshots (token_id,side,price,size,timestamp) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

    # ── 便捷写入 ──

    def insert_market(self, token_id: str, slug: str, question: str, category: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO markets (token_id,slug,question,category,last_updated) VALUES (?,?,?,?,?)",
            (token_id, slug, question, category, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def insert_relation(self, market_a: str, market_b: str, relation_type: str,
                        description: str, source: str = "llm_auto") -> int:
        cur = self.execute(
            "INSERT INTO relations (market_a,market_b,relation_type,description,source) VALUES (?,?,?,?,?)",
            (market_a, market_b, relation_type, description, source),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_relation_status(self, relation_id: int, status: str) -> None:
        self.execute("UPDATE relations SET status=? WHERE id=?", (status, relation_id))
        self._conn.commit()

    def get_approved_relations(self) -> list[dict]:
        cur = self.execute(
            "SELECT id,market_a,market_b,relation_type,description FROM relations WHERE status='approved'"
        )
        return [
            {"id": r[0], "market_a": r[1], "market_b": r[2], "relation_type": r[3], "description": r[4]}
            for r in cur.fetchall()
        ]

    def insert_violation(self, relation_id: int, price_a: float, price_b: float,
                         violation_amount: float, signal: str) -> int:
        cur = self.execute(
            "INSERT INTO violations (relation_id,price_a,price_b,violation_amount,signal) VALUES (?,?,?,?,?)",
            (relation_id, price_a, price_b, violation_amount, signal),
        )
        self._conn.commit()
        return cur.lastrowid

    def insert_sim_trade(self, violation_id: int, leg1_token: str, leg1_side: str,
                         leg1_price: float, leg1_size: float, leg2_token: str,
                         leg2_side: str, leg2_price: float, leg2_size: float,
                         expected_profit: float, status: str) -> int:
        cur = self.execute(
            "INSERT INTO sim_trades (violation_id,leg1_token,leg1_side,leg1_price,leg1_size,"
            "leg2_token,leg2_side,leg2_price,leg2_size,expected_profit,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (violation_id, leg1_token, leg1_side, leg1_price, leg1_size,
             leg2_token, leg2_side, leg2_price, leg2_size, expected_profit, status),
        )
        self._conn.commit()
        return cur.lastrowid

    def close(self) -> None:
        self.flush()
        if self._conn:
            self._conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_db.py::test_create_tables -v
```

Expected: PASS

- [ ] **Step 5: Write test for buffer flush**

Add to `tests/test_db.py`:

```python
def test_buffer_flush():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        for i in range(110):
            db.buffer_tick(f"token_{i}", 0.5, 0.55, 0.525, 0.05, 100.0, 100.0)
        db.flush()
        cur = db.execute("SELECT COUNT(*) FROM price_ticks")
        assert cur.fetchone()[0] == 110
        db.close()

def test_snapshot_partitioning():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        db.buffer_snapshot("token_1", "bids", 0.55, 100.0)
        db.flush()
        snapshot_files = list(Path(tmp).glob("snapshots_*.db"))
        assert len(snapshot_files) == 1
        db.close()
```

- [ ] **Step 6: Run all DB tests**

```bash
pytest tests/test_db.py -v
```

Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add SQLite database layer with WAL mode and batch buffering"
```

---

## Chunk 2: Market Discovery + LLM Integration

### Task 3: Market discovery via REST API

**Files:**
- Create: `market_discovery.py`
- Create: `tests/test_market_discovery.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_market_discovery.py
from market_discovery import MarketDiscovery

def test_filter_political_markets():
    """Test filtering logic with mock data"""
    raw = [
        {"condition_id": "abc", "question": "Trump wins?", "slug": "trump-wins",
         "category": "politics", "active": True, "tokens": [{"token_id": "t1", "outcome": "Yes"}]},
        {"condition_id": "def", "question": "Rain tomorrow?", "slug": "rain",
         "category": "weather", "active": True, "tokens": [{"token_id": "t2", "outcome": "Yes"}]},
        {"condition_id": "ghi", "question": "Harris wins?", "slug": "harris-wins",
         "category": "politics", "active": False, "tokens": [{"token_id": "t3", "outcome": "Yes"}]},
    ]
    md = MarketDiscovery(rest_endpoint="http://fake", category_filter="politics")
    filtered = md._filter_markets(raw)
    assert len(filtered) == 1
    assert filtered[0]["condition_id"] == "abc"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_market_discovery.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement MarketDiscovery**

Create: `market_discovery.py`

```python
"""
市场发现模块

- 通过 REST API 拉取 Polymarket 政治市场列表
- 过滤活跃市场
- 定期刷新发现新市场
"""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class MarketDiscovery:
    def __init__(
        self,
        rest_endpoint: str = "https://clob.polymarket.com",
        category_filter: str = "politics",
    ):
        self.rest_endpoint = rest_endpoint.rstrip("/")
        self.category_filter = category_filter

    def fetch_markets(self) -> list[dict]:
        """从 CLOB API 拉取市场列表并过滤"""
        try:
            url = f"{self.rest_endpoint}/markets"
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
            # API 可能返回 {"data": [...]} 或直接返回 [...]
            markets = raw if isinstance(raw, list) else raw.get("data", [])
            return self._filter_markets(markets)
        except requests.RequestException as e:
            logger.error("拉取市场列表失败: %s", e)
            return []

    def _filter_markets(self, raw: list[dict]) -> list[dict]:
        """过滤: 仅保留 active=True 且 category 匹配的市场"""
        return [
            m for m in raw
            if m.get("active") is True
            and m.get("category", "").lower() == self.category_filter.lower()
        ]

    def extract_token_ids(self, markets: list[dict]) -> list[dict[str, str]]:
        """
        从市场列表中提取 token_id 和元数据。

        返回: [{"token_id": "...", "slug": "...", "question": "...", "category": "..."}]
        """
        results = []
        for m in markets:
            tokens = m.get("tokens", [])
            for t in tokens:
                results.append({
                    "token_id": t.get("token_id", ""),
                    "slug": m.get("slug", ""),
                    "question": m.get("question", ""),
                    "category": m.get("category", ""),
                })
        return results

    def find_pairable_markets(self, markets: list[dict]) -> list[tuple[dict, dict]]:
        """
        按 slug 前缀分组，生成同组内的市场配对（用于 LLM 约束解析）。
        避免 N² 爆炸：只配对看起来相关的市场。

        相关性判定：共享关键词（州名、候选人名等）
        """
        # 按关键词分桶
        buckets: dict[str, list[dict]] = {}
        keywords = self._extract_keywords

        for m in markets:
            for kw in keywords(m.get("slug", "")):
                buckets.setdefault(kw, []).append(m)

        # 生成配对（去重）
        pairs = set()
        result = []
        for bucket in buckets.values():
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    key = (bucket[i]["condition_id"], bucket[j]["condition_id"])
                    rev = (bucket[j]["condition_id"], bucket[i]["condition_id"])
                    if key not in pairs and rev not in pairs:
                        pairs.add(key)
                        result.append((bucket[i], bucket[j]))

        return result

    @staticmethod
    def _extract_keywords(slug: str) -> list[str]:
        """从 slug 中提取关键词用于分组"""
        # 常见政治关键词
        political_terms = {
            "trump", "harris", "biden", "desantis", "republican", "democrat",
            "gop", "dem", "senate", "house", "governor", "electoral",
            "pennsylvania", "georgia", "michigan", "wisconsin", "arizona",
            "nevada", "north-carolina", "pa", "ga", "mi", "wi", "az", "nv", "nc",
        }
        parts = slug.lower().replace("-", " ").split()
        return [p for p in parts if p in political_terms]
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_market_discovery.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_discovery.py tests/test_market_discovery.py
git commit -m "feat: add market discovery with political market filtering and pairing"
```

---

### Task 4: LLM integration (Claude + Gemini)

**Files:**
- Create: `llm_parser.py`
- Create: `tests/test_llm_parser.py`

- [ ] **Step 1: Write failing test (mock LLM response)**

```python
# tests/test_llm_parser.py
from llm_parser import RelationParser, parse_llm_response

def test_parse_llm_response_subset():
    raw = '{"relation": "subset", "description": "GOP wins PA by 5% is subset of Trump wins PA"}'
    result = parse_llm_response(raw)
    assert result["relation"] == "subset"

def test_parse_llm_response_none():
    raw = '{"relation": "none", "description": "no logical relation"}'
    result = parse_llm_response(raw)
    assert result["relation"] == "none"

def test_parse_llm_response_invalid():
    raw = 'this is not json'
    result = parse_llm_response(raw)
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_llm_parser.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement LLM parser**

Create: `llm_parser.py`

```python
"""
LLM 市场关系解析模块

支持 Claude API 和 Gemini API，可通过 config 切换。
解析两个政治市场之间的逻辑关系 (subset / mutex / equivalent / none)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a political market analyst. Given two prediction market descriptions, determine their logical relationship.

Output ONLY a JSON object with:
- "relation": one of "subset", "mutex", "equivalent", "none"
- "description": brief explanation in English

Definitions:
- subset: Market B's YES outcome is a strict subset of Market A's YES outcome. If B happens, A must also happen. Example: "GOP wins PA by 5%+" is subset of "Trump wins PA"
- mutex: Markets A and B cannot both resolve YES. Example: "Trump wins" and "Harris wins"
- equivalent: Markets A and B will always resolve the same way. Example: two identical markets with different wording
- none: No clear logical relationship"""

_USER_TEMPLATE = """Market A: {question_a}
Market B: {question_b}

What is the logical relationship between these two markets?"""


def parse_llm_response(raw: str) -> dict | None:
    """Parse LLM response string into relation dict."""
    try:
        # Try direct JSON parse
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting JSON from markdown code blocks
    match = re.search(r'\{[^}]+\}', raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    logger.warning("无法解析 LLM 响应: %s", raw[:200])
    return None


class RelationParser:
    def __init__(self, provider: str = "claude", claude_api_key: str = "", gemini_api_key: str = ""):
        self.provider = provider
        self._claude_key = claude_api_key
        self._gemini_key = gemini_api_key

    def analyze_pair(self, question_a: str, question_b: str) -> dict | None:
        """
        调用 LLM 分析两个市场的逻辑关系。

        返回: {"relation": "subset"|"mutex"|"equivalent"|"none", "description": "..."}
              或 None（调用失败时）
        """
        prompt = _USER_TEMPLATE.format(question_a=question_a, question_b=question_b)

        if self.provider == "claude":
            raw = self._call_claude(prompt)
        elif self.provider == "gemini":
            raw = self._call_gemini(prompt)
        else:
            logger.error("未知 LLM provider: %s", self.provider)
            return None

        if raw is None:
            return None
        return parse_llm_response(raw)

    def _call_claude(self, prompt: str) -> str | None:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._claude_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            logger.error("Claude API 调用失败: %s", e)
            return None

    def _call_gemini(self, prompt: str) -> str | None:
        try:
            from google import genai
            client = genai.Client(api_key=self._gemini_key)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
            )
            return response.text
        except Exception as e:
            logger.error("Gemini API 调用失败: %s", e)
            return None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_llm_parser.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add llm_parser.py tests/test_llm_parser.py
git commit -m "feat: add LLM relation parser with Claude and Gemini support"
```

---

## Chunk 3: Engine Core

### Task 5: Engine orchestrator

**Files:**
- Create: `engine.py`
- Create: `tests/test_engine.py`

- [ ] **Step 1: Write failing test for engine signal detection**

```python
# tests/test_engine.py
import os, tempfile
from engine import Engine
from db import Database

def test_engine_detects_violation():
    """Engine should detect subset violation and record it"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        # Insert markets
        db.insert_market("token_a", "trump-wins-pa", "Will Trump win PA?", "politics")
        db.insert_market("token_b", "gop-wins-pa-5pct", "Will GOP win PA by 5%+?", "politics")
        # Insert approved relation: B ⊆ A
        rel_id = db.insert_relation("token_a", "token_b", "subset",
                                    "GOP 5%+ is subset of Trump wins", "manual")
        db.update_relation_status(rel_id, "approved")

        engine = Engine(db=db, config={"engine": {"min_profit_threshold": 0.05,
                                                   "kelly_max_depth_ratio": 0.5,
                                                   "leg2_recheck_delay": 0}})
        # Simulate prices: P(B) > P(A) — violation!
        prices = {"token_a": 0.50, "token_b": 0.60}
        violations = engine.check_constraints(prices)
        assert len(violations) == 1
        assert violations[0].violation_amount > 0
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_engine.py::test_engine_detects_violation -v
```

Expected: FAIL

- [ ] **Step 3: Implement Engine**

Create: `engine.py`

```python
"""
核心引擎

职责:
- 接收订单簿更新回调
- 执行约束校验
- 计算 VWAP + Kelly
- 触发模拟执行
- 写入数据库
"""
from __future__ import annotations

import logging
from typing import Any

from db import Database
from skills.data_stream.vwap import calculate_vwap, estimate_execution_probability
from skills.logic_engine.constraints import (
    ConstraintValidator,
    ConstraintViolation,
    MarketRelation,
    RelationType,
)
from skills.optimization.kelly import modified_kelly_fraction

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, db: Database, config: dict):
        self.db = db
        self.cfg = config["engine"]
        self.validator = ConstraintValidator()
        self._prices: dict[str, float] = {}
        self._orderbooks: dict[str, dict] = {}
        self._sim_balance: float = config.get("engine", {}).get("sim_initial_balance", 10000)

    def reload_relations(self) -> int:
        """从数据库加载已审核通过的约束"""
        approved = self.db.get_approved_relations()
        self.validator.relations.clear()
        for r in approved:
            self.validator.relations.append(MarketRelation(
                market_a=r["market_a"],
                market_b=r["market_b"],
                relation=RelationType(r["relation_type"]),
                description=r["description"],
            ))
        logger.info("已加载 %d 条约束", len(approved))
        return len(approved)

    def on_orderbook_update(self, token_id: str, book: dict) -> None:
        """
        WebSocket 订单簿更新回调。
        此方法由 DataStream 线程调用。
        """
        self._orderbooks[token_id] = book

        # 提取最优价并更新
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = bids[0]["price"]
            best_ask = asks[0]["price"]
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            bid_depth = sum(l["size"] for l in bids)
            ask_depth = sum(l["size"] for l in asks)

            self._prices[token_id] = mid

            # 缓冲写入 price_ticks
            self.db.buffer_tick(token_id, best_bid, best_ask, mid, spread, bid_depth, ask_depth)

            # 缓冲写入 orderbook_snapshots
            for lvl in bids:
                self.db.buffer_snapshot(token_id, "bids", lvl["price"], lvl["size"])
            for lvl in asks:
                self.db.buffer_snapshot(token_id, "asks", lvl["price"], lvl["size"])

        # 运行约束校验
        violations = self.check_constraints(self._prices)
        for v in violations:
            self._process_violation(v)

    def check_constraints(self, prices: dict[str, float]) -> list[ConstraintViolation]:
        """校验所有约束并记录日志"""
        violations = self.validator.check_violations(prices)

        # 记录每次校验到 check_logs
        for rel in self.validator.relations:
            pa = prices.get(rel.market_a)
            pb = prices.get(rel.market_b)
            if pa is not None and pb is not None:
                is_violated = any(
                    v.relation.market_a == rel.market_a and v.relation.market_b == rel.market_b
                    for v in violations
                )
                amount = 0.0
                for v in violations:
                    if v.relation.market_a == rel.market_a:
                        amount = v.violation_amount
                self.db.buffer_check(0, pa, pb, is_violated, amount)

        return violations

    def _process_violation(self, v: ConstraintViolation) -> None:
        """处理约束违反：VWAP 计算 → Kelly → 模拟执行"""
        book_a = self._orderbooks.get(v.relation.market_a, {})
        book_b = self._orderbooks.get(v.relation.market_b, {})

        asks_a = book_a.get("asks", [])
        bids_b = book_b.get("bids", [])

        if not asks_a or not bids_b:
            return

        # 试算 100 份的 VWAP
        test_size = 100.0
        vwap_buy = calculate_vwap(asks_a, test_size, side="buy")
        vwap_sell = calculate_vwap(bids_b, test_size, side="sell")

        if not vwap_buy.fully_filled or not vwap_sell.fully_filled:
            return

        # 实际利润
        profit = (vwap_sell.avg_price - vwap_buy.avg_price) * test_size
        if profit < self.cfg["min_profit_threshold"]:
            return

        # Kelly 仓位
        exec_prob = estimate_execution_probability(asks_a, test_size)
        profit_pct = profit / (vwap_buy.avg_price * test_size)
        kelly = modified_kelly_fraction(profit_pct, exec_prob, self.cfg["kelly_max_depth_ratio"])

        actual_size = min(test_size * kelly / 0.5, test_size)  # 按 Kelly 调整
        if actual_size <= 0:
            return

        # 记录违反
        vid = self.db.insert_violation(
            relation_id=0,
            price_a=v.price_a,
            price_b=v.price_b,
            violation_amount=v.violation_amount,
            signal=v.signal,
        )

        # 模拟交易记录
        self.db.insert_sim_trade(
            violation_id=vid,
            leg1_token=v.relation.market_a,
            leg1_side="buy",
            leg1_price=vwap_buy.avg_price,
            leg1_size=actual_size,
            leg2_token=v.relation.market_b,
            leg2_side="sell",
            leg2_price=vwap_sell.avg_price,
            leg2_size=actual_size,
            expected_profit=round(profit * (actual_size / test_size), 4),
            status="executed",
        )

        logger.info(
            "模拟交易: BUY %s @ %.4f, SELL %s @ %.4f, 利润 $%.4f",
            v.relation.market_a, vwap_buy.avg_price,
            v.relation.market_b, vwap_sell.avg_price,
            profit * (actual_size / test_size),
        )
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_engine.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine.py tests/test_engine.py
git commit -m "feat: add core engine with constraint checking, VWAP, and simulated execution"
```

---

## Chunk 4: Web API + Dashboard

### Task 6: FastAPI web server

**Files:**
- Create: `web/__init__.py`
- Create: `web/api.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: Write failing test for API endpoints**

```python
# tests/test_api.py
import os, tempfile
from fastapi.testclient import TestClient
from db import Database

def _make_app():
    tmp = tempfile.mkdtemp()
    db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
    db.init()
    db.insert_market("t1", "trump-wins", "Will Trump win?", "politics")
    from web.api import create_app
    return create_app(db), db

def test_get_status():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert "uptime" in resp.json()
    db.close()

def test_get_markets():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/markets")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    db.close()

def test_get_relations_empty():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/relations")
    assert resp.status_code == 200
    assert resp.json() == []
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_api.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement FastAPI app**

Create: `web/__init__.py` (empty)
Create: `web/api.py`

```python
"""
FastAPI Web API

提供 REST 接口 + 静态面板。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import Database

_start_time = time.monotonic()


class RelationAction(BaseModel):
    status: str  # "approved" | "rejected"
    relation_type: str | None = None  # 可选修改


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="Polymarket Arbitrage Dashboard")

    # ── Status ──

    @app.get("/api/status")
    def get_status():
        uptime = time.monotonic() - _start_time
        market_count = db.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        relation_count = db.execute("SELECT COUNT(*) FROM relations WHERE status='approved'").fetchone()[0]
        return {
            "uptime": round(uptime),
            "market_count": market_count,
            "relation_count": relation_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── Markets ──

    @app.get("/api/markets")
    def get_markets():
        rows = db.execute(
            "SELECT token_id,slug,question,category,active,last_price,last_updated FROM markets"
        ).fetchall()
        return [
            {"token_id": r[0], "slug": r[1], "question": r[2], "category": r[3],
             "active": bool(r[4]), "last_price": r[5], "last_updated": r[6]}
            for r in rows
        ]

    # ── Orderbook ──

    @app.get("/api/orderbook/{token_id}")
    def get_orderbook(token_id: str):
        # 从 engine 的内存中获取（通过 app.state）
        books = getattr(app.state, "orderbooks", {})
        book = books.get(token_id)
        if not book:
            return {"bids": [], "asks": []}
        return book

    # ── Relations ──

    @app.get("/api/relations")
    def get_relations(status: str | None = None):
        if status:
            rows = db.execute(
                "SELECT id,market_a,market_b,relation_type,description,status,created_at,source "
                "FROM relations WHERE status=?", (status,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id,market_a,market_b,relation_type,description,status,created_at,source FROM relations"
            ).fetchall()
        return [
            {"id": r[0], "market_a": r[1], "market_b": r[2], "relation_type": r[3],
             "description": r[4], "status": r[5], "created_at": r[6], "source": r[7]}
            for r in rows
        ]

    @app.post("/api/relations/{relation_id}")
    def update_relation(relation_id: int, action: RelationAction):
        existing = db.execute("SELECT id FROM relations WHERE id=?", (relation_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Relation not found")
        if action.relation_type:
            db.execute(
                "UPDATE relations SET relation_type=?, status=? WHERE id=?",
                (action.relation_type, action.status, relation_id),
            )
        else:
            db.update_relation_status(relation_id, action.status)
        return {"ok": True}

    # ── Violations ──

    @app.get("/api/violations")
    def get_violations(limit: int = 100):
        rows = db.execute(
            "SELECT id,relation_id,price_a,price_b,violation_amount,signal,detected_at "
            "FROM violations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"id": r[0], "relation_id": r[1], "price_a": r[2], "price_b": r[3],
             "violation_amount": r[4], "signal": r[5], "detected_at": r[6]}
            for r in rows
        ]

    # ── Trades ──

    @app.get("/api/trades")
    def get_trades(limit: int = 100):
        rows = db.execute(
            "SELECT id,violation_id,leg1_token,leg1_side,leg1_price,leg1_size,"
            "leg2_token,leg2_side,leg2_price,leg2_size,expected_profit,status,created_at "
            "FROM sim_trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {"id": r[0], "violation_id": r[1],
             "leg1": {"token": r[2], "side": r[3], "price": r[4], "size": r[5]},
             "leg2": {"token": r[6], "side": r[7], "price": r[8], "size": r[9]},
             "expected_profit": r[10], "status": r[11], "created_at": r[12]}
            for r in rows
        ]

    # ── Stats ──

    @app.get("/api/stats")
    def get_stats():
        total_trades = db.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0]
        total_profit = db.execute(
            "SELECT COALESCE(SUM(expected_profit),0) FROM sim_trades WHERE status='executed'"
        ).fetchone()[0]
        today_trades = db.execute(
            "SELECT COUNT(*) FROM sim_trades WHERE created_at >= date('now')"
        ).fetchone()[0]
        today_violations = db.execute(
            "SELECT COUNT(*) FROM violations WHERE detected_at >= date('now')"
        ).fetchone()[0]
        return {
            "total_trades": total_trades,
            "total_profit": round(total_profit, 4),
            "today_trades": today_trades,
            "today_violations": today_violations,
        }

    # ── Price History ──

    @app.get("/api/price-history/{token_id}")
    def get_price_history(token_id: str, limit: int = 500):
        rows = db.execute(
            "SELECT mid_price,spread,bid_depth,ask_depth,timestamp FROM price_ticks "
            "WHERE token_id=? ORDER BY id DESC LIMIT ?", (token_id, limit)
        ).fetchall()
        return [
            {"mid_price": r[0], "spread": r[1], "bid_depth": r[2],
             "ask_depth": r[3], "timestamp": r[4]}
            for r in reversed(rows)  # 时间正序
        ]

    return app
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_api.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/ tests/test_api.py
git commit -m "feat: add FastAPI web API with all REST endpoints"
```

---

### Task 7: Web dashboard frontend

**Files:**
- Create: `web/static/index.html`
- Create: `web/static/app.js`
- Create: `web/static/style.css`

- [ ] **Step 1: Create dashboard HTML**

Create: `web/static/index.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 套利监控</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <header>
        <h1>Polymarket 套利监控</h1>
        <div id="status-bar">
            <span id="ws-status">⏳ 连接中...</span>
            <span id="market-count">市场: -</span>
            <span id="uptime">运行: -</span>
        </div>
    </header>
    <div class="layout">
        <nav>
            <a href="#" data-page="dashboard" class="active">Dashboard</a>
            <a href="#" data-page="markets">Markets</a>
            <a href="#" data-page="relations">Relations</a>
            <a href="#" data-page="signals">Signals</a>
            <a href="#" data-page="trades">Trades</a>
        </nav>
        <main>
            <section id="page-dashboard">
                <div class="cards">
                    <div class="card"><h3>监控市场</h3><p id="stat-markets">-</p></div>
                    <div class="card"><h3>活跃约束</h3><p id="stat-relations">-</p></div>
                    <div class="card"><h3>今日信号</h3><p id="stat-violations">-</p></div>
                    <div class="card"><h3>今日交易</h3><p id="stat-trades">-</p></div>
                    <div class="card accent"><h3>累计模拟盈亏</h3><p id="stat-profit">-</p></div>
                </div>
                <h2>最近信号</h2>
                <table id="recent-signals"><thead><tr>
                    <th>时间</th><th>偏差</th><th>信号</th>
                </tr></thead><tbody></tbody></table>
            </section>
            <section id="page-markets" class="hidden">
                <h2>监控中的市场</h2>
                <table id="market-table"><thead><tr>
                    <th>Slug</th><th>问题</th><th>最新价</th><th>更新时间</th>
                </tr></thead><tbody></tbody></table>
            </section>
            <section id="page-relations" class="hidden">
                <h2>逻辑约束</h2>
                <div class="tabs">
                    <button data-filter="pending" class="active">待审核</button>
                    <button data-filter="approved">已通过</button>
                    <button data-filter="">全部</button>
                </div>
                <table id="relation-table"><thead><tr>
                    <th>ID</th><th>Market A</th><th>Market B</th><th>关系</th><th>说明</th><th>操作</th>
                </tr></thead><tbody></tbody></table>
            </section>
            <section id="page-signals" class="hidden">
                <h2>套利信号历史</h2>
                <table id="signal-table"><thead><tr>
                    <th>时间</th><th>P(A)</th><th>P(B)</th><th>偏差</th><th>信号</th>
                </tr></thead><tbody></tbody></table>
            </section>
            <section id="page-trades" class="hidden">
                <h2>模拟交易记录</h2>
                <table id="trade-table"><thead><tr>
                    <th>时间</th><th>Leg1</th><th>Leg2</th><th>利润</th><th>状态</th>
                </tr></thead><tbody></tbody></table>
            </section>
        </main>
    </div>
    <footer id="log-bar">最近日志加载中...</footer>
    <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create CSS**

Create: `web/static/style.css`

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', 'Menlo', monospace; background: #0a0a0f; color: #e0e0e0; font-size: 13px; }
header { background: #12121a; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e1e2e; }
header h1 { font-size: 16px; color: #7c7cff; }
#status-bar span { margin-left: 16px; color: #888; font-size: 12px; }
.layout { display: flex; height: calc(100vh - 80px); }
nav { width: 140px; background: #12121a; padding: 16px 0; border-right: 1px solid #1e1e2e; }
nav a { display: block; padding: 8px 20px; color: #888; text-decoration: none; font-size: 13px; }
nav a.active, nav a:hover { color: #7c7cff; background: #1a1a2e; }
main { flex: 1; padding: 20px; overflow-y: auto; }
section.hidden { display: none; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
.card { background: #16161f; border: 1px solid #1e1e2e; border-radius: 8px; padding: 16px; }
.card h3 { font-size: 11px; color: #666; margin-bottom: 8px; text-transform: uppercase; }
.card p { font-size: 24px; font-weight: bold; color: #e0e0e0; }
.card.accent p { color: #4caf50; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th { text-align: left; padding: 8px; color: #666; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #1e1e2e; }
td { padding: 8px; border-bottom: 1px solid #111; font-size: 12px; }
tr:hover { background: #16161f; }
.tabs { margin-bottom: 12px; }
.tabs button { background: #1a1a2e; border: 1px solid #2a2a3e; color: #888; padding: 6px 14px; margin-right: 4px; cursor: pointer; border-radius: 4px; font-size: 12px; }
.tabs button.active { color: #7c7cff; border-color: #7c7cff; }
button.approve { background: #1a3a1a; color: #4caf50; border: 1px solid #2a5a2a; padding: 4px 10px; cursor: pointer; border-radius: 3px; margin-right: 4px; }
button.reject { background: #3a1a1a; color: #f44336; border: 1px solid #5a2a2a; padding: 4px 10px; cursor: pointer; border-radius: 3px; }
footer { background: #0d0d14; padding: 8px 24px; font-size: 11px; color: #555; border-top: 1px solid #1e1e2e; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
```

- [ ] **Step 3: Create JavaScript**

Create: `web/static/app.js`

```javascript
const API = '';

// ── Navigation ──
document.querySelectorAll('nav a').forEach(a => {
    a.addEventListener('click', e => {
        e.preventDefault();
        document.querySelectorAll('nav a').forEach(x => x.classList.remove('active'));
        a.classList.add('active');
        document.querySelectorAll('main > section').forEach(s => s.classList.add('hidden'));
        document.getElementById('page-' + a.dataset.page).classList.remove('hidden');
    });
});

// ── Data Fetching ──
async function fetchJSON(url) {
    const resp = await fetch(API + url);
    return resp.json();
}

async function refreshStatus() {
    const s = await fetchJSON('/api/status');
    document.getElementById('market-count').textContent = '市场: ' + s.market_count;
    document.getElementById('uptime').textContent = '运行: ' + Math.floor(s.uptime / 60) + 'm';
    document.getElementById('ws-status').textContent = '🟢 运行中';
}

async function refreshStats() {
    const s = await fetchJSON('/api/stats');
    document.getElementById('stat-markets').textContent = s.total_trades >= 0 ? document.getElementById('stat-markets').textContent : '-';
    document.getElementById('stat-violations').textContent = s.today_violations;
    document.getElementById('stat-trades').textContent = s.today_trades;
    document.getElementById('stat-profit').textContent = '$' + s.total_profit.toFixed(2);

    const status = await fetchJSON('/api/status');
    document.getElementById('stat-markets').textContent = status.market_count;
    document.getElementById('stat-relations').textContent = status.relation_count;
}

async function refreshSignals() {
    const data = await fetchJSON('/api/violations?limit=20');
    const tbody = document.querySelector('#recent-signals tbody');
    tbody.innerHTML = data.map(v =>
        `<tr><td>${v.detected_at || '-'}</td><td>${v.violation_amount.toFixed(4)}</td><td>${v.signal}</td></tr>`
    ).join('');
}

async function refreshMarkets() {
    const data = await fetchJSON('/api/markets');
    const tbody = document.querySelector('#market-table tbody');
    tbody.innerHTML = data.map(m =>
        `<tr><td>${m.slug}</td><td>${m.question}</td><td>${m.last_price ?? '-'}</td><td>${m.last_updated || '-'}</td></tr>`
    ).join('');
}

async function refreshRelations(status) {
    const url = status ? `/api/relations?status=${status}` : '/api/relations';
    const data = await fetchJSON(url);
    const tbody = document.querySelector('#relation-table tbody');
    tbody.innerHTML = data.map(r =>
        `<tr>
            <td>${r.id}</td><td>${r.market_a}</td><td>${r.market_b}</td>
            <td>${r.relation_type}</td><td>${r.description}</td>
            <td>${r.status === 'pending' ?
                `<button class="approve" onclick="approveRelation(${r.id})">Approve</button>` +
                `<button class="reject" onclick="rejectRelation(${r.id})">Reject</button>` : r.status
            }</td>
        </tr>`
    ).join('');
}

async function refreshTrades() {
    const data = await fetchJSON('/api/trades');
    const tbody = document.querySelector('#trade-table tbody');
    tbody.innerHTML = data.map(t =>
        `<tr>
            <td>${t.created_at || '-'}</td>
            <td>${t.leg1.side.toUpperCase()} @ ${t.leg1.price.toFixed(4)}</td>
            <td>${t.leg2.side.toUpperCase()} @ ${t.leg2.price.toFixed(4)}</td>
            <td>$${t.expected_profit.toFixed(4)}</td>
            <td>${t.status}</td>
        </tr>`
    ).join('');
}

// ── Relation Actions ──
async function approveRelation(id) {
    await fetch(`/api/relations/${id}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'approved'})
    });
    refreshRelations(currentRelationFilter);
}

async function rejectRelation(id) {
    await fetch(`/api/relations/${id}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'rejected'})
    });
    refreshRelations(currentRelationFilter);
}

// ── Relation Filter Tabs ──
let currentRelationFilter = 'pending';
document.querySelectorAll('.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentRelationFilter = btn.dataset.filter;
        refreshRelations(currentRelationFilter);
    });
});

// ── Refresh Loop (every 2 seconds) ──
async function refreshAll() {
    try {
        await Promise.all([refreshStatus(), refreshStats(), refreshSignals()]);
    } catch (e) {
        document.getElementById('ws-status').textContent = '🔴 断开';
    }
}

refreshAll();
refreshMarkets();
refreshRelations('pending');
refreshTrades();

setInterval(refreshAll, 2000);
setInterval(refreshMarkets, 10000);
setInterval(() => refreshRelations(currentRelationFilter), 5000);
setInterval(refreshTrades, 5000);
```

- [ ] **Step 4: Mount static files in FastAPI**

Modify: `web/api.py` — add at the end of `create_app()`, before `return app`:

```python
    # ── Static files ──
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(static_dir / "index.html"))
```

Also add `from pathlib import Path` to imports.

- [ ] **Step 5: Commit**

```bash
git add web/static/
git commit -m "feat: add web dashboard with real-time monitoring UI"
```

---

## Chunk 5: Main Orchestrator + Integration

### Task 8: Main entry point

**Files:**
- Create: `main.py`

- [ ] **Step 1: Implement main.py**

```python
"""
Polymarket 套利系统主入口

启动三个线程:
  1. DataStream — WebSocket 订单簿数据
  2. Engine — 约束校验 + 模拟执行
  3. Web — FastAPI 面板
"""
from __future__ import annotations

import argparse
import logging
import threading
import time

import uvicorn

from config import load_config
from db import Database
from engine import Engine
from market_discovery import MarketDiscovery
from skills.data_stream.stream import PolymarketStream
from web.api import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage System")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger.info("配置已加载: %s", args.config)

    # ── 初始化数据库 ──
    db = Database(
        main_db=cfg["storage"]["main_db"],
        snapshots_dir=cfg["storage"]["snapshots_dir"],
    )
    db.init()
    logger.info("数据库已初始化")

    # ── 初始化引擎 ──
    engine = Engine(db=db, config=cfg)

    # ── 市场发现 ──
    discovery = MarketDiscovery(
        rest_endpoint=cfg["polymarket"]["rest_endpoint"],
        category_filter=cfg["polymarket"]["category_filter"],
    )

    # ── 初始化 WebSocket 数据流 ──
    stream = PolymarketStream(endpoint=cfg["polymarket"]["ws_endpoint"])

    def discover_and_subscribe():
        """发现市场并订阅"""
        markets = discovery.fetch_markets()
        tokens = discovery.extract_token_ids(markets)
        for t in tokens:
            db.insert_market(t["token_id"], t["slug"], t["question"], t["category"])
            stream.subscribe(t["token_id"], on_book_update=engine.on_orderbook_update)
        engine.reload_relations()
        logger.info("已发现 %d 个市场, %d 个 token", len(markets), len(tokens))

    # 首次发现
    discover_and_subscribe()

    # ── 定时刷新线程 ──
    def refresh_loop():
        interval = cfg["polymarket"]["market_refresh_interval"]
        while True:
            time.sleep(interval)
            try:
                discover_and_subscribe()
            except Exception as e:
                logger.error("市场刷新失败: %s", e)

    refresh_thread = threading.Thread(target=refresh_loop, name="Refresher", daemon=True)
    refresh_thread.start()

    # ── 启动 WebSocket (Thread-1) ──
    stream.start_async()
    logger.info("WebSocket 数据流已启动")

    # ── 启动 Web (Thread-3) ──
    app = create_app(db)
    app.state.orderbooks = stream.orderbooks  # 共享订单簿引用

    web_host = cfg["web"]["host"]
    web_port = cfg["web"]["port"]
    logger.info("Web 面板启动: http://%s:%d", web_host, web_port)

    # Uvicorn 在主线程运行 (阻塞)
    uvicorn.run(app, host=web_host, port=web_port, log_level="warning")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test manual startup**

```bash
cd /Users/jackyz/Documents/poly
python main.py --config config.yaml
```

Expected: Server starts, logs show market discovery attempt, Web panel available at `http://127.0.0.1:8080`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add main orchestrator with 3-thread architecture"
```

---

### Task 9: Integration smoke test

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
"""
端到端集成测试 (不需要真实 API 连接)
"""
import os, tempfile
from db import Database
from engine import Engine
from skills.logic_engine.constraints import ConstraintViolation, MarketRelation, RelationType
from fastapi.testclient import TestClient
from web.api import create_app


def test_full_pipeline():
    """模拟完整流程: 插入市场 → 添加约束 → 审核 → 注入价格 → 检测违反 → 查询 API"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()

        # 1. 插入市场
        db.insert_market("ta", "trump-pa", "Trump wins PA?", "politics")
        db.insert_market("tb", "gop-pa-5", "GOP wins PA by 5%+?", "politics")

        # 2. 添加约束 (模拟 LLM 输出)
        rid = db.insert_relation("ta", "tb", "subset", "B ⊆ A", "llm_auto")

        # 3. 审核通过 (通过 API)
        app = create_app(db)
        client = TestClient(app)
        resp = client.post(f"/api/relations/{rid}", json={"status": "approved"})
        assert resp.status_code == 200

        # 4. 引擎加载约束
        engine = Engine(db=db, config={"engine": {
            "min_profit_threshold": 0.05, "kelly_max_depth_ratio": 0.5,
            "leg2_recheck_delay": 0, "sim_initial_balance": 10000,
        }})
        count = engine.reload_relations()
        assert count == 1

        # 5. 注入违反价格
        violations = engine.check_constraints({"ta": 0.50, "tb": 0.60})
        assert len(violations) == 1

        # 6. 验证 API 可查询
        resp = client.get("/api/relations?status=approved")
        assert len(resp.json()) == 1

        db.close()
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_integration.py -v
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration test for full pipeline"
```

---

## Summary

| Chunk | Tasks | 产出 |
|-------|-------|------|
| **1: Foundation** | Task 1-2 | Git repo, config, DB layer |
| **2: Discovery + LLM** | Task 3-4 | 市场发现, Claude/Gemini 解析 |
| **3: Engine** | Task 5 | 核心引擎 (约束→VWAP→Kelly→模拟) |
| **4: Web** | Task 6-7 | FastAPI API + 暗色调面板 |
| **5: Integration** | Task 8-9 | main.py 三线程启动 + 端到端测试 |

**依赖关系:** Task 1 → Task 2 → Task 3/4 (并行) → Task 5 → Task 6 → Task 7 → Task 8 → Task 9
