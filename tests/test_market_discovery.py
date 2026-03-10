from market_discovery import MarketDiscovery

def test_filter_political_markets():
    """Test filtering logic with mock data"""
    raw = [
        {"condition_id": "abc", "question": "Trump wins?", "slug": "trump-wins",
         "category": "politics", "active": True, "tokens": [{"token_id": "t1", "outcome": "Yes"}]},
        {"condition_id": "def", "question": "Rain tomorrow?", "slug": "rain",
         "category": "weather", "active": True, "tokens": [{"token_id": "t2", "outcome": "Yes"}]},
        {"condition_id": "ghi", "question": "Harris wins?", "slug": "harris-wins",
         "category": "politics", "active": False, "tokens": [{"token_id": "t3", "outcome": "Yes"}]},
    ]
    md = MarketDiscovery(rest_endpoint="http://fake", category_filter="politics")
    filtered = md._filter_markets(raw)
    assert len(filtered) == 1
    assert filtered[0]["condition_id"] == "abc"
