"""
端到端集成测试 (不需要真实 API 连接)
"""
import os, tempfile
from db import Database
from engine import Engine
from fastapi.testclient import TestClient
from web.api import create_app


def test_full_pipeline():
    """模拟完整流程: 插入市场 → 添加约束 → 审核 → 引擎加载 → 注入价格 → 检测违反 → 查询 API"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()

        # 1. 插入市场
        db.insert_market("ta", "trump-pa", "Trump wins PA?", "politics")
        db.insert_market("tb", "gop-pa-5", "GOP wins PA by 5%+?", "politics")

        # 2. 添加约束 (模拟 LLM 输出)
        rid = db.insert_relation("ta", "tb", "subset", "B ⊆ A", "llm_auto")

        # 3. 审核通过 (通过 API)
        app = create_app(db)
        client = TestClient(app)
        resp = client.post(f"/api/relations/{rid}", json={"status": "approved"})
        assert resp.status_code == 200

        # 4. 引擎加载约束
        engine = Engine(db=db, config={"engine": {
            "min_profit_threshold": 0.05, "kelly_max_depth_ratio": 0.5,
            "leg2_recheck_delay": 0, "sim_initial_balance": 10000,
        }})
        count = engine.reload_relations()
        assert count == 1

        # 5. 注入违反价格
        violations = engine.check_constraints({"ta": 0.50, "tb": 0.60})
        assert len(violations) == 1

        # 6. 验证 API 可查询
        resp = client.get("/api/relations?status=approved")
        assert len(resp.json()) == 1

        resp = client.get("/api/markets")
        assert len(resp.json()) == 2

        resp = client.get("/api/stats")
        assert resp.status_code == 200

        db.close()
