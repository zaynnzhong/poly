import os, tempfile
from engine import Engine
from db import Database

def test_engine_detects_violation():
    """Engine should detect subset violation and record it"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        # Insert markets
        db.insert_market("token_a", "trump-wins-pa", "Will Trump win PA?", "politics")
        db.insert_market("token_b", "gop-wins-pa-5pct", "Will GOP win PA by 5%+?", "politics")
        # Insert approved relation: B ⊆ A
        rel_id = db.insert_relation("token_a", "token_b", "subset",
                                    "GOP 5%+ is subset of Trump wins", "manual")
        db.update_relation_status(rel_id, "approved")

        engine = Engine(db=db, config={"engine": {"min_profit_threshold": 0.05,
                                                   "kelly_max_depth_ratio": 0.5,
                                                   "leg2_recheck_delay": 0}})
        engine.reload_relations()
        # Simulate prices: P(B) > P(A) — violation!
        prices = {"token_a": 0.50, "token_b": 0.60}
        violations = engine.check_constraints(prices)
        assert len(violations) == 1
        assert violations[0].violation_amount > 0
        db.close()

def test_engine_no_violation_when_valid():
    """No violation when P(B) <= P(A) for subset"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        db.insert_market("token_a", "trump-wins-pa", "Will Trump win PA?", "politics")
        db.insert_market("token_b", "gop-wins-pa-5pct", "Will GOP win PA by 5%+?", "politics")
        rel_id = db.insert_relation("token_a", "token_b", "subset",
                                    "GOP 5%+ is subset of Trump wins", "manual")
        db.update_relation_status(rel_id, "approved")

        engine = Engine(db=db, config={"engine": {"min_profit_threshold": 0.05,
                                                   "kelly_max_depth_ratio": 0.5,
                                                   "leg2_recheck_delay": 0}})
        engine.reload_relations()
        # P(B) < P(A) — no violation
        violations = engine.check_constraints({"token_a": 0.60, "token_b": 0.40})
        assert len(violations) == 0
        db.close()
