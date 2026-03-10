import os, tempfile
from fastapi.testclient import TestClient
from db import Database

def _make_app():
    tmp = tempfile.mkdtemp()
    db = Database(main_db=os.path.join(tmp, "test.db"), snapshots_dir=tmp)
    db.init()
    db.insert_market("t1", "trump-wins", "Will Trump win?", "politics")
    from web.api import create_app
    return create_app(db), db

def test_get_status():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert "uptime" in resp.json()
    db.close()

def test_get_markets():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/markets")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    db.close()

def test_get_relations_empty():
    app, db = _make_app()
    client = TestClient(app)
    resp = client.get("/api/relations")
    assert resp.status_code == 200
    assert resp.json() == []
    db.close()
