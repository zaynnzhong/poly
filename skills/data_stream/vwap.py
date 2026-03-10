"""
VWAP (Volume-Weighted Average Price) 计算模块

核心准则：绝不以盘口最优价作为参考。
必须根据订单簿深度，计算实际买入/卖出特定数量代币的加权平均成本。
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass
class VWAPResult:
    """VWAP 计算结果"""
    avg_price: float        # 加权平均成交价
    total_cost: float       # 总花费 (USDC)
    filled_amount: float    # 实际可成交数量
    fully_filled: bool      # 是否完全成交
    depth_ratio: float      # 占订单簿深度的比例 (用于仓位管理)
    price_levels_used: int  # 消耗了几个价位档


def calculate_vwap(
    orderbook: list[dict],
    amount: float,
    side: Literal["buy", "sell"] = "buy",
) -> VWAPResult:
    """
    根据订单簿深度，计算买入/卖出指定数量份额的真实加权平均成本。

    参数:
        orderbook: 订单簿列表，每个元素为 {"price": float, "size": float}。
                   - buy 时传入 asks（卖单），按 price 升序排列
                   - sell 时传入 bids（买单），按 price 降序排列
        amount:    目标成交数量（份额数）
        side:      "buy" 或 "sell"

    返回:
        VWAPResult 包含加权平均价、总成本、实际成交量等

    示例:
        >>> asks = [{"price": 0.55, "size": 100}, {"price": 0.57, "size": 200}]
        >>> result = calculate_vwap(asks, 150, side="buy")
        >>> # 100 份 @ 0.55 + 50 份 @ 0.57 => avg ≈ 0.5567
    """
    if not orderbook or amount <= 0:
        return VWAPResult(
            avg_price=0.0,
            total_cost=0.0,
            filled_amount=0.0,
            fully_filled=False,
            depth_ratio=0.0,
            price_levels_used=0,
        )

    # 确保排序：buy 按价格升序（吃最便宜的 ask），sell 按价格降序（吃最高的 bid）
    sorted_book = sorted(
        orderbook,
        key=lambda x: x["price"],
        reverse=(side == "sell"),
    )

    total_depth = sum(level["size"] for level in sorted_book)
    remaining = amount
    weighted_sum = 0.0
    total_cost = 0.0
    levels_used = 0

    for level in sorted_book:
        price = level["price"]
        size = level["size"]

        fill = min(remaining, size)
        cost = fill * price

        weighted_sum += cost
        total_cost += cost
        remaining -= fill
        levels_used += 1

        if remaining <= 0:
            break

    filled_amount = amount - max(remaining, 0)
    avg_price = weighted_sum / filled_amount if filled_amount > 0 else 0.0

    return VWAPResult(
        avg_price=round(avg_price, 6),
        total_cost=round(total_cost, 4),
        filled_amount=filled_amount,
        fully_filled=(remaining <= 0),
        depth_ratio=round(filled_amount / total_depth, 4) if total_depth > 0 else 0.0,
        price_levels_used=levels_used,
    )


def estimate_execution_probability(
    orderbook: list[dict],
    amount: float,
) -> float:
    """
    估算完全执行概率（用于改良 Kelly 公式）。

    基于订单簿深度与目标数量的比值，使用 sigmoid 映射为 [0, 1] 概率。
    当 amount 占深度 < 30% 时概率较高，> 70% 时概率骤降。
    """
    total_depth = sum(level["size"] for level in orderbook)
    if total_depth == 0:
        return 0.0

    ratio = amount / total_depth
    # sigmoid 映射: ratio=0.5 → prob≈0.5, ratio=0.2 → prob≈0.88
    prob = 1.0 / (1.0 + np.exp(10 * (ratio - 0.5)))
    return round(float(prob), 4)
