"""
FastAPI Web API

提供 REST 接口 + 静态面板。
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from db import Database

_start_time = time.monotonic()


class RelationAction(BaseModel):
    status: str  # "approved" | "rejected"
    relation_type: Optional[str] = None  # 可选修改


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
    def get_relations(status: Optional[str] = None):
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

    # ── Static files ──
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(static_dir / "index.html"))

    return app
