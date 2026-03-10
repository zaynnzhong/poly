"""
改良 Kelly 公式 — 仓位管理模块

标准 Kelly: f* = (b*p - q) / b
改良版: 用「完全执行概率」替代胜率 p，因为套利本身是确定性利润，
       真正的风险来自于多腿订单能否全部成交。
"""

from __future__ import annotations


def modified_kelly_fraction(
    profit_pct: float,
    execution_prob: float,
    max_depth_ratio: float = 0.5,
) -> float:
    """
    计算改良 Kelly 公式下的最优投入比例。

    参数:
        profit_pct:     套利利润百分比 (如 0.03 = 3%)
        execution_prob: 完全执行概率 (由订单簿深度估算, 0~1)
        max_depth_ratio: 最大深度占比上限 (默认 50%)

    返回:
        建议投入占总资金的比例 (0~1)
    """
    if profit_pct <= 0 or execution_prob <= 0:
        return 0.0

    # 改良 Kelly: f* = (b*p - q) / b，其中 b=profit_pct, p=exec_prob, q=1-exec_prob
    # 即: f = exec_prob - (1 - exec_prob) / profit_pct
    kelly = execution_prob - (1 - execution_prob) / profit_pct
    if kelly <= 0:
        return 0.0
    # 使用半 Kelly 降低波动
    half_kelly = kelly * 0.5
    # 硬性上限：不超过深度的 max_depth_ratio
    return min(half_kelly, max_depth_ratio)
