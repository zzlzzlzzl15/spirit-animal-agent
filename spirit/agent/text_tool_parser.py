"""Text tool call parser.

Parses XML/JSON-style tool calls from providers that do not support
native OpenAI tool_calls format (e.g. MiniMax, Copilot).

Supports:
1. Hermes/Copilot style: 
2. Bare JSON fallback (when no XML tags found)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hermes/Copilot style: 
_TOOL_CALL_BLOCK_RE = re.compile(
    r'\x3ctool_call\x3e\s*([\s\S]*?)\s*\x3c/tool_call\x3e',
    re.DOTALL,
)

# MiniMax style: 
_MINIMAX_INVOKE_RE = re.compile(
    r'\x3cinvoke\s+name\s*=\s*["\']([^"\']+)["\']\s*\x3e(.*?)\x3c/invoke\x3e',
    re.DOTALL | re.IGNORECASE,
)

_MINIMAX_PARAM_RE = re.compile(
    r'\x3cparameter\s+name\s*=\s*["\']([^"\']+)["\']\s*\x3e(.*?)\x3c/parameter\x3e',
    re.DOTALL | re.IGNORECASE,
)

# Bare JSON fallback
_TOOL_CALL_JSON_RE = re.compile(
    r'\{\s*"id"\s*:\s*"[^"]+"\s*,\s*"type"\s*:\s*"function"\s*,\s*"function"\s*:\s*\{.*?\}\s*\}',
    re.DOTALL,
)

# MiniMax style: 
_MINIMAX_INVOKE_RE = re.compile(
    r'\x3cinvoke\s+name\s*=\s*["\']([^"\']+)["\']\s*\x3e(.*?)\x3c/invoke\x3e',
    re.DOTALL | re.IGNORECASE,
)

_MINIMAX_PARAM_RE = re.compile(
    r'\x3cparameter\s+name\s*=\s*["\']([^"\']+)["\']\s*\x3e(.*?)\x3c/parameter\x3e',
    re.DOTALL | re.IGNORECASE,
)


def _build_openai_tool_call(
    call_id: str,
    name: str,
    arguments: str,
) -> Dict[str, Any]:
    """Build an OpenAI-compatible tool-call dict."""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }



def _parse_minimax_invoke(match, call_counter):
    """Parse MiniMax invoke block."""
    name = match.group(1).strip()
    body = match.group(2).strip()
    params = {}
    for param_match in _MINIMAX_PARAM_RE.finditer(body):
        param_name = param_match.group(1).strip()
        param_value = param_match.group(2).strip()
        try:
            parsed_value = json.loads(param_value)
            params[param_name] = parsed_value
        except (ValueError, Exception):
            params[param_name] = param_value
    if not params:
        return None
    call_id = f'minimax_call_{call_counter}'
    arguments_str = json.dumps(params, ensure_ascii=False)
    return _build_openai_tool_call(call_id, name, arguments_str)

def parse_text_tool_calls(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse tool calls from text.

    Priority:
    1. 
    2. Bare JSON fallback (only when no XML blocks found)

    Returns:
        (tool_calls, cleaned_text): tuple of parsed tool calls and text with tool call blocks removed.
    """
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: List[Dict[str, Any]] = []
    consumed_spans: List[Tuple[int, int]] = []

    def _try_add_obj(obj: Any) -> bool:
        """Try to add a single parsed JSON object as a tool call."""
        if not isinstance(obj, dict):
            return False
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return False
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return False
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{len(extracted) + 1}"

        extracted.append(
            _build_openai_tool_call(
                call_id=call_id,
                name=fn_name.strip(),
                arguments=fn_args,
            )
        )
        return True

    def _try_add_tool_call(raw_json: str) -> None:
        # 1) 单个 JSON 对象 / JSON 数组
        try:
            obj = json.loads(raw_json)
        except Exception:
            obj = None
        if obj is not None:
            if isinstance(obj, list):
                for item in obj:
                    _try_add_obj(item)
                return
            if _try_add_obj(obj):
                return
        # 2) 块内含多个连续 JSON 对象 — 用 raw_decode 逐个扫描
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(raw_json):
            start = raw_json.find("{", idx)
            if start < 0:
                break
            try:
                obj, end = decoder.raw_decode(raw_json[start:])
            except Exception:
                break
            _try_add_obj(obj)
            idx = start + end

    # 1. Try 
    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        before = len(extracted)
        _try_add_tool_call(raw)
        # 块被识别为工具调用块即消费（避免原始 JSON 进入上下文）
        if len(extracted) > before or raw.strip().startswith("{"):
            consumed_spans.append((m.start(), m.end()))

    # 2. Bare JSON fallback (only when no XML blocks found)
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))


    # 3. MiniMax style: 
    call_counter = 0
    for m in _MINIMAX_INVOKE_RE.finditer(text):
        parsed_call = _parse_minimax_invoke(m, call_counter)
        if parsed_call:
            extracted.append(parsed_call)
            consumed_spans.append((m.start(), m.end()))
            call_counter += 1
    if not consumed_spans:
        return extracted, text.strip()

    # Remove consumed spans from text
    consumed_spans.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: List[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned
