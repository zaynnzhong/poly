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
