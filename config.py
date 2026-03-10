from __future__ import annotations
import os
from pathlib import Path
import yaml

_DEFAULT_PATH = Path(__file__).parent / "config.yaml"

def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else _DEFAULT_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # env var overrides for secrets
    cfg["llm"]["claude_api_key"] = os.getenv("CLAUDE_API_KEY", cfg["llm"]["claude_api_key"])
    cfg["llm"]["gemini_api_key"] = os.getenv("GEMINI_API_KEY", cfg["llm"]["gemini_api_key"])
    return cfg
