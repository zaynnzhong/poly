"""
套利执行器 (Skill_Executor)

职责：
- 非原子性下单流控制
- Leg 1 成交后监控 Leg 2 价格变化
- 利润消失时紧急停止
- 完整执行日志
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 硬性门槛：利润低于此值不执行
MIN_PROFIT_THRESHOLD = 0.05


class LegStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderLeg:
    """单腿订单"""
    leg_id: int
    token_id: str
    side: str               # "buy" or "sell"
    price: float
    size: float
    status: LegStatus = LegStatus.PENDING
    fill_price: float | None = None
    fill_time: datetime | None = None
    order_id: str | None = None


@dataclass
class ExecutionLog:
    """执行记录"""
    timestamp: datetime
    event: str
    details: dict[str, Any] = field(default_factory=dict)


class ArbitrageExecutor:
    """
    非原子套利执行器。

    核心流程：
    1. 预检：利润 >= MIN_PROFIT_THRESHOLD ($0.05)
    2. 提交 Leg 1
    3. Leg 1 成交后，重新检查 Leg 2 的实时价格
    4. 若利润仍然存在 → 提交 Leg 2
    5. 若利润消失 → 停止执行，记录日志，必要时平仓 Leg 1
    """

    def __init__(
        self,
        submit_order_fn: Callable[[str, str, float, float], str],
        get_price_fn: Callable[[str], float],
        cancel_order_fn: Callable[[str], bool] | None = None,
    ):
        """
        参数:
            submit_order_fn: 下单函数 (token_id, side, price, size) -> order_id
            get_price_fn:    获取实时价格 (token_id) -> price
            cancel_order_fn: 撤单函数 (order_id) -> success
        """
        self._submit = submit_order_fn
        self._get_price = get_price_fn
        self._cancel = cancel_order_fn
        self.logs: list[ExecutionLog] = []

    def execute_pair(
        self,
        leg1: OrderLeg,
        leg2: OrderLeg,
        expected_profit: float,
        recheck_delay: float = 0.5,
    ) -> bool:
        """
        执行双腿套利交易。

        参数:
            leg1:            第一条腿
            leg2:            第二条腿
            expected_profit: 预期利润 (USDC)
            recheck_delay:   Leg1 成交后等待多久再检查 Leg2 价格 (秒)

        返回:
            True = 双腿全部成交, False = 中途停止
        """
        # ── 预检 ──
        if expected_profit < MIN_PROFIT_THRESHOLD:
            self._log("REJECTED", {
                "reason": f"利润 ${expected_profit:.4f} < 阈值 ${MIN_PROFIT_THRESHOLD}",
                "leg1": leg1.token_id,
                "leg2": leg2.token_id,
            })
            return False

        self._log("START", {
            "expected_profit": expected_profit,
            "leg1": f"{leg1.side} {leg1.token_id} @ {leg1.price} x {leg1.size}",
            "leg2": f"{leg2.side} {leg2.token_id} @ {leg2.price} x {leg2.size}",
        })

        # ── Leg 1: 提交 ──
        try:
            leg1.order_id = self._submit(
                leg1.token_id, leg1.side, leg1.price, leg1.size
            )
            leg1.status = LegStatus.SUBMITTED
            self._log("LEG1_SUBMITTED", {"order_id": leg1.order_id})
        except Exception as e:
            leg1.status = LegStatus.FAILED
            self._log("LEG1_FAILED", {"error": str(e)})
            return False

        # 模拟等待成交确认（实际应通过 WebSocket 回调）
        leg1.status = LegStatus.FILLED
        leg1.fill_price = leg1.price
        leg1.fill_time = datetime.now(timezone.utc)
        self._log("LEG1_FILLED", {"fill_price": leg1.fill_price})

        # ── 关键检查点：Leg 1 成交后重新评估 Leg 2 ──
        time.sleep(recheck_delay)

        current_price_2 = self._get_price(leg2.token_id)
        recalc_profit = self._recalculate_profit(
            leg1, leg2, current_price_2
        )

        self._log("RECHECK", {
            "leg2_original_price": leg2.price,
            "leg2_current_price": current_price_2,
            "recalc_profit": recalc_profit,
        })

        if recalc_profit < MIN_PROFIT_THRESHOLD:
            # ── 利润消失：紧急停止 ──
            self._log("ABORT", {
                "reason": f"重算利润 ${recalc_profit:.4f} < 阈值，停止 Leg2",
                "exposure": f"{leg1.side} {leg1.size} @ {leg1.fill_price} 未对冲",
            })
            leg2.status = LegStatus.CANCELLED
            return False

        # ── Leg 2: 用最新价格提交 ──
        leg2.price = current_price_2
        try:
            leg2.order_id = self._submit(
                leg2.token_id, leg2.side, leg2.price, leg2.size
            )
            leg2.status = LegStatus.SUBMITTED
            self._log("LEG2_SUBMITTED", {"order_id": leg2.order_id})
        except Exception as e:
            leg2.status = LegStatus.FAILED
            self._log("LEG2_FAILED", {"error": str(e)})
            return False

        leg2.status = LegStatus.FILLED
        leg2.fill_price = leg2.price
        leg2.fill_time = datetime.now(timezone.utc)
        self._log("LEG2_FILLED", {"fill_price": leg2.fill_price})

        actual_profit = self._recalculate_profit(leg1, leg2, leg2.fill_price)
        self._log("COMPLETE", {"actual_profit": actual_profit})

        return True

    def _recalculate_profit(
        self,
        leg1: OrderLeg,
        leg2: OrderLeg,
        leg2_price: float,
    ) -> float:
        """根据 Leg1 实际成交价和 Leg2 当前价格重算利润"""
        leg1_cost = (leg1.fill_price or leg1.price) * leg1.size
        leg2_cost = leg2_price * leg2.size

        if leg1.side == "buy" and leg2.side == "sell":
            return round(leg2_cost - leg1_cost, 4)
        elif leg1.side == "sell" and leg2.side == "buy":
            return round(leg1_cost - leg2_cost, 4)
        else:
            # 同方向（如两个 sell for mutex）：卖出两个 YES，最多赔付 max(size) * $1
            max_payout = max(leg1.size, leg2.size)
            return round(leg1_cost + leg2_cost - max_payout, 4)

    def _log(self, event: str, details: dict[str, Any] | None = None) -> None:
        entry = ExecutionLog(
            timestamp=datetime.now(timezone.utc),
            event=event,
            details=details or {},
        )
        self.logs.append(entry)
        logger.info("[%s] %s %s", entry.timestamp.isoformat(), event, details or "")

    def get_logs(self) -> list[dict]:
        """导出执行日志"""
        return [
            {
                "timestamp": log.timestamp.isoformat(),
                "event": log.event,
                "details": log.details,
            }
            for log in self.logs
        ]
