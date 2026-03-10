from llm_parser import RelationParser, parse_llm_response

def test_parse_llm_response_subset():
    raw = '{"relation": "subset", "description": "GOP wins PA by 5% is subset of Trump wins PA"}'
    result = parse_llm_response(raw)
    assert result["relation"] == "subset"

def test_parse_llm_response_none():
    raw = '{"relation": "none", "description": "no logical relation"}'
    result = parse_llm_response(raw)
    assert result["relation"] == "none"

def test_parse_llm_response_invalid():
    raw = 'this is not json'
    result = parse_llm_response(raw)
    assert result is None

def test_parse_llm_response_markdown_code_block():
    raw = '```json\n{"relation": "mutex", "description": "mutually exclusive"}\n```'
    result = parse_llm_response(raw)
    assert result["relation"] == "mutex"
