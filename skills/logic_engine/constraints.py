"""
政治市场逻辑约束引擎 (Skill_Logic)

核心功能：
- 解析 LLM 输出的市场依赖关系 JSON
- 校验子集关系: P(B) <= P(A) 当 B ⊆ A
- 校验互斥关系: P(A_Yes) + P(B_Yes) <= 1
- 发现违反约束的定价偏差（即套利机会）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RelationType(Enum):
    SUBSET = "subset"           # B ⊆ A → P(B) <= P(A)
    MUTUAL_EXCLUSIVE = "mutex"  # A ∩ B = ∅ → P(A) + P(B) <= 1
    EQUIVALENT = "equivalent"   # A ≡ B → P(A) ≈ P(B)


@dataclass
class MarketRelation:
    """两个市场之间的逻辑关系"""
    market_a: str              # 市场 A 的 token_id 或标识符
    market_b: str              # 市场 B 的 token_id 或标识符
    relation: RelationType     # 逻辑关系类型
    description: str = ""      # 人类可读描述（如 "共和党大胜宾州 ⊆ 特朗普赢宾州"）


@dataclass
class ConstraintViolation:
    """约束违反记录 = 套利信号"""
    relation: MarketRelation
    price_a: float
    price_b: float
    violation_amount: float    # 偏差幅度
    signal: str                # 交易方向建议


class ConstraintValidator:
    """
    逻辑约束校验器。

    接收 LLM 解析出的市场依赖关系 JSON，持续校验实时价格是否违反约束。
    """

    def __init__(self) -> None:
        self.relations: list[MarketRelation] = []

    def load_relations_json(self, json_input: str | list[dict]) -> int:
        """
        加载 LLM 输出的市场依赖关系。

        JSON 格式:
        [
            {
                "market_a": "token_id_trump_wins_pa",
                "market_b": "token_id_gop_wins_pa_by_5",
                "relation": "subset",
                "description": "共和党在宾州赢5%以上 ⊆ 特朗普赢下宾州"
            },
            {
                "market_a": "token_id_trump_wins",
                "market_b": "token_id_harris_wins",
                "relation": "mutex",
                "description": "特朗普获胜 与 哈里斯获胜 互斥"
            }
        ]

        返回: 成功加载的关系数量
        """
        if isinstance(json_input, str):
            data = json.loads(json_input)
        else:
            data = json_input

        count = 0
        for item in data:
            try:
                rel = MarketRelation(
                    market_a=item["market_a"],
                    market_b=item["market_b"],
                    relation=RelationType(item["relation"]),
                    description=item.get("description", ""),
                )
                self.relations.append(rel)
                count += 1
            except (KeyError, ValueError) as e:
                logger.warning("跳过无效关系: %s, 错误: %s", item, e)

        logger.info("已加载 %d 条逻辑约束", count)
        return count

    def check_violations(
        self,
        prices: dict[str, float],
        tolerance: float = 0.005,
    ) -> list[ConstraintViolation]:
        """
        校验当前价格是否违反已知逻辑约束。

        参数:
            prices:    {token_id: current_price} 映射
            tolerance: 容忍偏差（避免微小浮动误报），默认 0.5%

        返回:
            违反约束的列表（每一条都是潜在套利信号）
        """
        violations = []

        for rel in self.relations:
            pa = prices.get(rel.market_a)
            pb = prices.get(rel.market_b)

            if pa is None or pb is None:
                continue

            violation = self._check_single(rel, pa, pb, tolerance)
            if violation:
                violations.append(violation)

        return violations

    def _check_single(
        self,
        rel: MarketRelation,
        pa: float,
        pb: float,
        tol: float,
    ) -> ConstraintViolation | None:

        if rel.relation == RelationType.SUBSET:
            # B ⊆ A → P(B) <= P(A)
            # 违反: P(B) > P(A) → 做空 B，做多 A
            if pb > pa + tol:
                return ConstraintViolation(
                    relation=rel,
                    price_a=pa,
                    price_b=pb,
                    violation_amount=round(pb - pa, 4),
                    signal=f"BUY {rel.market_a} (YES) @ {pa}, SELL {rel.market_b} (YES) @ {pb}",
                )

        elif rel.relation == RelationType.MUTUAL_EXCLUSIVE:
            # A ∩ B = ∅ → P(A) + P(B) <= 1
            # 违反: P(A) + P(B) > 1 → 做空两者的 YES
            total = pa + pb
            if total > 1.0 + tol:
                return ConstraintViolation(
                    relation=rel,
                    price_a=pa,
                    price_b=pb,
                    violation_amount=round(total - 1.0, 4),
                    signal=f"SELL {rel.market_a} (YES) @ {pa}, SELL {rel.market_b} (YES) @ {pb}",
                )

        elif rel.relation == RelationType.EQUIVALENT:
            # A ≡ B → |P(A) - P(B)| ≈ 0
            diff = abs(pa - pb)
            if diff > tol:
                if pa > pb:
                    signal = f"SELL {rel.market_a} @ {pa}, BUY {rel.market_b} @ {pb}"
                else:
                    signal = f"BUY {rel.market_a} @ {pa}, SELL {rel.market_b} @ {pb}"
                return ConstraintViolation(
                    relation=rel,
                    price_a=pa,
                    price_b=pb,
                    violation_amount=round(diff, 4),
                    signal=signal,
                )

        return None
