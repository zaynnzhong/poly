import os, tempfile
from pathlib import Path
from db import Database

def test_create_tables():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {row[0] for row in tables}
        assert "markets" in names
        assert "relations" in names
        assert "violations" in names
        assert "sim_trades" in names
        assert "price_ticks" in names
        assert "check_logs" in names
        db.close()

def test_buffer_flush():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        for i in range(110):
            db.buffer_tick(f"token_{i}", 0.5, 0.55, 0.525, 0.05, 100.0, 100.0)
        db.flush()
        cur = db.execute("SELECT COUNT(*) FROM price_ticks")
        assert cur.fetchone()[0] == 110
        db.close()

def test_snapshot_partitioning():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
        db.init()
        db.buffer_snapshot("token_1", "bids", 0.55, 100.0)
        db.flush()
        snapshot_files = list(Path(tmp).glob("snapshots_*.db"))
        assert len(snapshot_files) == 1
        db.close()
