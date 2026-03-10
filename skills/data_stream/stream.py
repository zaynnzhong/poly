"""
Polymarket CLOB WebSocket 实时数据流模块

职责：
- 订阅政治板块订单簿更新
- 维护本地订单簿快照
- 成交推送回调
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket

_lock = threading.Lock()

logger = logging.getLogger(__name__)

# Polymarket CLOB WebSocket 端点
WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketStream:
    """
    Polymarket CLOB WebSocket 数据流客户端。

    用法:
        stream = PolymarketStream()
        stream.subscribe(
            token_id="<condition_token_id>",
            on_book_update=my_callback,
        )
        stream.start()  # 阻塞，或 start_async() 后台运行
    """

    def __init__(self, endpoint: str = WS_ENDPOINT):
        self.endpoint = endpoint
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._subscriptions: dict[str, list[Callable]] = {}
        # 本地订单簿快照: token_id -> {"bids": [...], "asks": [...]}
        self.orderbooks: dict[str, dict] = {}
        self._running = False

    def subscribe(
        self,
        token_id: str,
        on_book_update: Callable[[str, dict], None] | None = None,
    ) -> None:
        """
        注册订阅。

        参数:
            token_id:       Polymarket condition token ID
            on_book_update: 回调函数，签名 (token_id, orderbook_snapshot) -> None
        """
        if on_book_update:
            self._subscriptions.setdefault(token_id, []).append(on_book_update)
        self.orderbooks.setdefault(token_id, {"bids": [], "asks": []})

    def start(self) -> None:
        """阻塞式启动 WebSocket 连接"""
        self._running = True
        self._connect()

    def start_async(self) -> None:
        """后台线程启动"""
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """断开连接"""
        self._running = False
        if self._ws:
            self._ws.close()

    # ── WebSocket 回调 ──────────────────────────────────────────

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            self.endpoint,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        while self._running:
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error("WebSocket 异常: %s，5 秒后重连", e)
            if self._running:
                time.sleep(5)

    def _on_open(self, ws: websocket.WebSocket) -> None:
        logger.info("WebSocket 已连接: %s", self.endpoint)
        # 发送订阅消息
        for token_id in self._subscriptions:
            msg = json.dumps({
                "type": "subscribe",
                "channel": "book",
                "assets_id": token_id,
            })
            ws.send(msg)
            logger.info("已订阅 token: %s", token_id)

    def _on_message(self, ws: websocket.WebSocket, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("无法解析消息: %s", message[:200])
            return

        event_type = data.get("event_type") or data.get("type", "")
        asset_id = data.get("asset_id", "")

        if event_type in ("book", "book_update") and asset_id in self.orderbooks:
            with _lock:
                self._update_orderbook(asset_id, data)
                snapshot = self.orderbooks[asset_id].copy()
            # 触发回调（锁外执行，避免死锁）
            for cb in self._subscriptions.get(asset_id, []):
                try:
                    cb(asset_id, snapshot)
                except Exception as e:
                    logger.error("回调异常 [%s]: %s", asset_id, e)

    def _on_error(self, ws: websocket.WebSocket, error: Exception) -> None:
        logger.error("WebSocket 错误: %s", error)

    def _on_close(self, ws: websocket.WebSocket, code: int, reason: str) -> None:
        logger.info("WebSocket 已断开: code=%s reason=%s", code, reason)

    # ── 订单簿维护 ──────────────────────────────────────────────

    def _update_orderbook(self, token_id: str, data: dict) -> None:
        """
        增量更新本地订单簿快照。

        Polymarket CLOB 推送格式:
        {
            "bids": [{"price": "0.55", "size": "100"}, ...],
            "asks": [{"price": "0.56", "size": "200"}, ...]
        }
        """
        book = self.orderbooks[token_id]

        for side_key in ("bids", "asks"):
            updates = data.get(side_key, [])
            if not updates:
                continue

            # 转为 {price: size} 映射后合并
            current = {lvl["price"]: lvl["size"] for lvl in book[side_key]}
            for lvl in updates:
                price = float(lvl["price"])
                size = float(lvl["size"])
                if size == 0:
                    current.pop(price, None)  # 删除空档位
                else:
                    current[price] = size

            # 重建排序列表
            reverse = (side_key == "bids")
            book[side_key] = sorted(
                [{"price": p, "size": s} for p, s in current.items()],
                key=lambda x: x["price"],
                reverse=reverse,
            )

    def get_snapshot(self, token_id: str) -> dict | None:
        """获取指定 token 的当前订单簿快照（线程安全）"""
        with _lock:
            book = self.orderbooks.get(token_id)
            return book.copy() if book else None
