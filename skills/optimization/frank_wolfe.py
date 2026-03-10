"""
Frank-Wolfe 条件梯度法 — 套利最优买入量求解器

═══════════════════════════════════════════════════════════════════════════════
背景与数学框架
═══════════════════════════════════════════════════════════════════════════════

在政治选举套利中，我们面对 N 个相互关联的二元市场，每个市场有 YES/NO 两种代币。
当前市场价格向量 p ∈ R^{2N} 可能落在"无套利多面体"之外，此时存在正期望值交易。

目标：找到最优交易量向量 x*，使得以最小成本将市场价格"投影"回无套利空间，
同时最大化我们在这个过程中捕获的利润。

═══════════════════════════════════════════════════════════════════════════════
KL 散度 (Kullback-Leibler Divergence)
═══════════════════════════════════════════════════════════════════════════════

用于衡量当前价格分布 p 与最近的无套利价格分布 q 之间的"距离"：

    D_KL(p || q) = Σ_i p_i * ln(p_i / q_i)

在套利场景中的物理意义：
- p = 当前市场隐含概率向量（由交易价格推导）
- q = 满足所有逻辑约束（子集、互斥等）的最近合法概率分布
- D_KL 越大 → 市场定价偏差越严重 → 套利空间越大

我们使用 Bregman 投影（以 KL 散度为距离度量）将 p 投影到无套利集合 C 上：
    q* = argmin_{q ∈ C} D_KL(p || q)

═══════════════════════════════════════════════════════════════════════════════
整数规划 (Integer Programming) 约束
═══════════════════════════════════════════════════════════════════════════════

政治选举结果的特殊性质 — 结果是离散的（某州要么红要么蓝）：

1. 合法顶点 (Feasible Vertices):
   - 每个州的选举结果是二元的: x_state ∈ {0, 1}
   - 全国结果由各州结果组合决定
   - 合法顶点数量 = 2^(州数)，但逻辑约束会大幅缩减

2. 约束矩阵 A:
   - 子集约束: x_subset <= x_superset (对所有合法结果)
   - 互斥约束: x_a + x_b <= 1
   - 全局约束: Σ(各候选人概率) = 1 (对每个选举)

3. Gurobi 求解器接口 (预留):
   - 输入: 约束矩阵 A, 当前价格 p
   - 输出: 合法顶点集合 V = {v_1, ..., v_K}
   - 用途: 为 Frank-Wolfe 的线性子问题提供可行域

═══════════════════════════════════════════════════════════════════════════════
Frank-Wolfe 算法流程
═══════════════════════════════════════════════════════════════════════════════

Frank-Wolfe (条件梯度法) 适用于此场景的原因：
- 可行域是多面体（由逻辑约束定义），FW 只需在顶点上做线性优化
- 避免了投影步骤的高计算成本
- 解自然是稀疏的（只在少数顶点上有权重），对应少量交易

算法步骤：

    输入: 当前价格 p, 合法顶点集 V, 迭代次数 T
    初始化: x_0 = p (从当前价格出发)

    for t = 0, 1, ..., T-1:
        1. 计算梯度:
           ∇f(x_t) = ∇D_KL(p || x_t) = -p / x_t + 1
           (KL 散度对 q 的梯度)

        2. 线性子问题 (Linear Minimization Oracle):
           s_t = argmin_{s ∈ V} <∇f(x_t), s>
           → 在合法顶点集 V 上找梯度方向最小的顶点
           → 此步由 Gurobi 求解（当顶点集过大时）

        3. 步长选择:
           γ_t = 2 / (t + 2)  (经典递减步长)
           或通过精确线搜索确定

        4. 更新:
           x_{t+1} = (1 - γ_t) * x_t + γ_t * s_t

    输出: x_T (投影后的无套利价格)
    套利方向: p - x_T (哪些代币高估/低估)

═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageResult:
    """Frank-Wolfe 求解结果"""
    projected_prices: np.ndarray   # 投影后的无套利价格向量
    trade_direction: np.ndarray    # 交易方向 (正=买入, 负=卖出)
    kl_divergence: float           # 原始价格与投影点的 KL 散度
    profit_estimate: float         # 预估利润 (基于价格偏差总和)
    iterations_used: int           # 实际迭代次数
    converged: bool                # 是否收敛


def frank_wolfe_arbitrage(
    current_prices: np.ndarray,
    feasible_vertices: np.ndarray,
    max_iter: int = 200,
    tol: float = 1e-6,
) -> ArbitrageResult:
    """
    使用 Frank-Wolfe 条件梯度法，将当前市场价格投影到无套利可行域。

    参数:
        current_prices:    当前市场价格向量 p ∈ R^n, 每个元素 ∈ (0, 1)
        feasible_vertices: 合法顶点矩阵, shape=(K, n), 每行是一个合法结果向量
                           由整数规划 / Gurobi 预计算得到
        max_iter:          最大迭代次数
        tol:               收敛阈值 (对偶间隙 < tol 时停止)

    返回:
        ArbitrageResult 包含投影价格、交易方向、KL 散度等

    注意:
        - current_prices 中不应有 0 或 1（会导致 KL 散度无穷大），
          实际使用时应 clip 到 [ε, 1-ε]
        - feasible_vertices 需要由 Skill_Logic + Gurobi 预先枚举

    TODO (待数学家填充):
        1. 接入 Gurobi 求解器，实现 _linear_minimization_oracle
           当顶点数过大时，不枚举顶点而是直接求解 LP
        2. 实现自适应步长 (line search over KL divergence)
        3. 添加 away-step 变体以加速收敛
        4. 处理多市场耦合约束的分解策略
    """
    eps = 1e-8
    p = np.clip(current_prices, eps, 1 - eps)
    n = len(p)

    # 初始化：从可行域中选择离当前价格最近的顶点
    x = feasible_vertices[
        np.argmin(np.sum((feasible_vertices - p) ** 2, axis=1))
    ].astype(float)
    x = np.clip(x, eps, 1 - eps)

    converged = False
    final_iter = max_iter

    for t in range(max_iter):
        # ── Step 1: 计算 KL 散度的梯度 ∇D_KL(p || x) ──
        grad = -p / x + 1.0

        # ── Step 2: 线性极小化预言机 (LMO) ──
        # 在所有合法顶点中找梯度内积最小的
        # TODO: 当顶点集过大时，替换为 Gurobi LP 求解
        dots = feasible_vertices @ grad
        s = feasible_vertices[np.argmin(dots)]

        # ── 对偶间隙检查 (收敛条件) ──
        duality_gap = grad @ (x - s)
        if duality_gap < tol:
            converged = True
            final_iter = t + 1
            break

        # ── Step 3: 步长 ──
        gamma = 2.0 / (t + 2)

        # ── Step 4: 更新 ──
        x = (1 - gamma) * x + gamma * s
        x = np.clip(x, eps, 1 - eps)

    # 计算最终 KL 散度
    kl_div = float(np.sum(p * np.log(p / x)))

    # 交易方向：正值 = 市场高估（应卖出），负值 = 市场低估（应买入）
    trade_dir = p - x

    return ArbitrageResult(
        projected_prices=x,
        trade_direction=trade_dir,
        kl_divergence=round(kl_div, 6),
        profit_estimate=round(float(np.sum(np.maximum(trade_dir, 0))), 4),
        iterations_used=final_iter,
        converged=converged,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gurobi 求解器接口 (预留)
# ═══════════════════════════════════════════════════════════════════════════

def enumerate_feasible_vertices_gurobi(
    constraint_matrix: np.ndarray,
    constraint_rhs: np.ndarray,
    num_markets: int,
) -> np.ndarray:
    """
    使用 Gurobi 枚举所有合法结果顶点。

    TODO: 由数学家实现

    参数:
        constraint_matrix: 约束矩阵 A, shape=(m, n)
            - 每行定义一个线性约束: A[i] @ x <= b[i]
            - 包含子集约束、互斥约束、归一化约束
        constraint_rhs:    约束右端向量 b, shape=(m,)
        num_markets:       市场数量 n

    返回:
        feasible_vertices: shape=(K, n), K 个合法顶点

    实现思路:
        1. 定义二元变量 x_i ∈ {0, 1}
        2. 添加约束 A @ x <= b
        3. 使用 Gurobi 的 PoolSearchMode 枚举所有可行解
        4. 或使用 PoolSolutions 参数限制最大枚举数

    示例约束 (2024 美国大选):
        - 子集: x_gop_pa_5pct <= x_trump_pa
        - 互斥: x_trump_wins + x_harris_wins <= 1
        - 归一化: x_trump_wins + x_harris_wins + x_other = 1
    """
    raise NotImplementedError(
        "Gurobi 求解器接口待实现。需要: pip install gurobipy + 有效许可证"
    )
