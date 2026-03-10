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
