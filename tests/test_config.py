from config import load_config

def test_load_default_config():
    cfg = load_config()
    assert cfg["polymarket"]["ws_endpoint"].startswith("wss://")
    assert cfg["engine"]["min_profit_threshold"] == 0.05
    assert cfg["web"]["port"] == 8080
