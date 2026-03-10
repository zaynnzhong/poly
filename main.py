"""
Polymarket 套利系统主入口

启动三个线程:
  1. DataStream — WebSocket 订单簿数据
  2. Refresher — 定时发现新市场
  3. Web — FastAPI 面板 (主线程)
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

    # ── 启动 Web (主线程) ──
    app = create_app(db)
    app.state.orderbooks = stream.orderbooks  # 共享订单簿引用

    web_host = cfg["web"]["host"]
    web_port = cfg["web"]["port"]
    logger.info("Web 面板启动: http://%s:%d", web_host, web_port)

    # Uvicorn 在主线程运行 (阻塞)
    uvicorn.run(app, host=web_host, port=web_port, log_level="warning")


if __name__ == "__main__":
    main()
