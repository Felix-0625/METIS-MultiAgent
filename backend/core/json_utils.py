"""Shared robust JSON extraction for LLM output.

Replaces the greedy-regex pattern ``re.search(r'\\{.*\\}', content, re.DOTALL)``
which breaks when model output contains markdown fences, trailing prose, or
braces inside explanatory text. Uses ``JSONDecoder.raw_decode`` to scan each
``{`` / ``[`` position and decode one complete JSON value, so surrounding prose
no longer corrupts parsing.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n```", re.MULTILINE)


def _matches_shape(data: Any, want: str) -> bool:
    if want == "any":
        return True
    if want == "object":
        return isinstance(data, dict)
    if want == "array":
        return isinstance(data, list)
    return True


def extract_first_json(content: Any, *, want: str = "any") -> Optional[Any]:
    """Return the first complete JSON value in ``content``.

    Args:
        content: raw model text, possibly with prose/markdown around the JSON.
        want: ``"object"`` | ``"array"`` | ``"any"`` — restrict the top-level
            shape. ``"object"`` skips a bare top-level array and the objects
            nested inside it, matching the prior PMLeaderAgent behaviour.

    Returns the parsed value, or ``None`` when no valid JSON is found.
    """
    if not isinstance(content, str):
        return None
    raw = content.strip()
    if not raw:
        return None

    candidates = [raw]
    fence = _FENCE_RE.search(raw)
    if fence:
        candidates.insert(0, fence.group(1))

    decoder = json.JSONDecoder()
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            data = json.loads(cand)
            if _matches_shape(data, want):
                return data
        except Exception:
            pass
        for m in re.finditer(r"[\{\[]", cand):
            try:
                data, _end = decoder.raw_decode(cand, m.start())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if _matches_shape(data, want):
                return data
    return None


def extract_first_json_object(content: Any) -> Optional[dict]:
    """Convenience wrapper returning the first JSON object (dict) or None."""
    data = extract_first_json(content, want="object")
    return data if isinstance(data, dict) else None


def extract_first_json_array(content: Any) -> Optional[list]:
    """Convenience wrapper returning the first JSON array (list) or None."""
    data = extract_first_json(content, want="array")
    return data if isinstance(data, list) else None


__all__ = [
    "extract_first_json",
    "extract_first_json_object",
    "extract_first_json_array",
]
