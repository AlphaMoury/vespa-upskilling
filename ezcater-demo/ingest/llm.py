"""
Optional LLM provider (OpenAI). Every function degrades to None when no key is set,
so callers always have a deterministic fallback path.

Two entry points:
  chat_json(system, user)                 -> dict | None   (structured extraction)
  vision_json(system, user, images_b64)   -> dict | None   (PDF page understanding)

Callers enforce the cost policy: enrich.py / pdf.py gate on config.index_llm_enabled(),
server query-understanding gates on config.query_llm_enabled(). This module only knows
"is there a key and did the call succeed".
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_VISION_MODEL, has_key

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI  # type: ignore
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _parse_json(txt: str) -> Optional[dict]:
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return None
    return None


def chat_json(system: str, user: str, *, model: Optional[str] = None,
              max_tokens: int = 600) -> Optional[dict]:
    """Text -> structured JSON. Returns None if no key or the call fails."""
    if not has_key():
        return None
    try:
        resp = _get_client().chat.completions.create(
            model=model or OPENAI_MODEL,
            max_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        return None


def vision_json(system: str, user: str, images_b64: list[str], *,
                model: Optional[str] = None, max_tokens: int = 1500) -> Optional[dict]:
    """Page image(s) + instructions -> structured JSON. None if no key / failure.

    images_b64: raw base64 (no data: prefix) of PNG page rasters.
    """
    if not has_key() or not images_b64:
        return None
    content = [{"type": "text", "text": user}]
    for b in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b}", "detail": "high"},
        })
    try:
        resp = _get_client().chat.completions.create(
            model=model or OPENAI_VISION_MODEL,
            max_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        return None
