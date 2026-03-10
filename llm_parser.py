"""
LLM 市场关系解析模块

支持 Claude API 和 Gemini API，可通过 config 切换。
解析两个政治市场之间的逻辑关系 (subset / mutex / equivalent / none)。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a political market analyst. Given two prediction market descriptions, determine their logical relationship.

Output ONLY a JSON object with:
- "relation": one of "subset", "mutex", "equivalent", "none"
- "description": brief explanation in English

Definitions:
- subset: Market B's YES outcome is a strict subset of Market A's YES outcome. If B happens, A must also happen. Example: "GOP wins PA by 5%+" is subset of "Trump wins PA"
- mutex: Markets A and B cannot both resolve YES. Example: "Trump wins" and "Harris wins"
- equivalent: Markets A and B will always resolve the same way. Example: two identical markets with different wording
- none: No clear logical relationship"""

_USER_TEMPLATE = """Market A: {question_a}
Market B: {question_b}

What is the logical relationship between these two markets?"""


def parse_llm_response(raw: str) -> dict | None:
    """Parse LLM response string into relation dict."""
    try:
        # Try direct JSON parse
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting JSON from markdown code blocks
    match = re.search(r'\{[^}]+\}', raw)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    logger.warning("无法解析 LLM 响应: %s", raw[:200])
    return None


class RelationParser:
    def __init__(self, provider: str = "claude", claude_api_key: str = "", gemini_api_key: str = ""):
        self.provider = provider
        self._claude_key = claude_api_key
        self._gemini_key = gemini_api_key

    def analyze_pair(self, question_a: str, question_b: str) -> dict | None:
        """
        调用 LLM 分析两个市场的逻辑关系。

        返回: {"relation": "subset"|"mutex"|"equivalent"|"none", "description": "..."}
              或 None（调用失败时）
        """
        prompt = _USER_TEMPLATE.format(question_a=question_a, question_b=question_b)

        if self.provider == "claude":
            raw = self._call_claude(prompt)
        elif self.provider == "gemini":
            raw = self._call_gemini(prompt)
        else:
            logger.error("未知 LLM provider: %s", self.provider)
            return None

        if raw is None:
            return None
        return parse_llm_response(raw)

    def _call_claude(self, prompt: str) -> str | None:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._claude_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            logger.error("Claude API 调用失败: %s", e)
            return None

    def _call_gemini(self, prompt: str) -> str | None:
        try:
            from google import genai
            client = genai.Client(api_key=self._gemini_key)
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
            )
            return response.text
        except Exception as e:
            logger.error("Gemini API 调用失败: %s", e)
            return None
