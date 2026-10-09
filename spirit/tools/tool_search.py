"""Tool Search 桥接工具集（Hermes Progressive Tool Disclosure 同款）。

Spirit 默认可注册 21 个 toolset、~60 个工具，完整 schema 累计 ~40KB。
直接全量塞进每次 API 请求会让 MiniMax 等上游触发 529 overloaded。

**做法**：开关开启后，对 LLM 只暴露 3 个元工具替代 60+ 工具的位置：

- ``tool_search(query)``  — 按关键词模糊搜工具 name + 描述，返回候选清单
- ``tool_describe(name)`` — 拿到指定工具的完整 OpenAI function schema
- ``tool_call(name, args)`` — 直接调工具（不需要先 describe，模型可盲调）

并在 system prompt 末尾追加一份**精简 manifest**（仅 name + toolset + 一句话描述），
按 token 预算（``manifest_budget_pct`` 占 context 的比例）降级裁剪。LLM 想用工具时
先 ``tool_search`` 定位，再 ``tool_describe`` 看参数，必要时 ``tool_call`` 执行。

**触发条件**：
- ``agent.tool_search.enabled = true``
- 工具总数 > ``eager_threshold``（默认 15，低于此数直接发所有 schema 更省事）

**向后兼容**：
- 关闭时所有现有路径保持不变
- 开启时仍允许 LLM 直接调原始工具（registry.dispatch 兜底），不强制走 bridge
- 3 个 bridge 工具的 toolset 名是 ``"tool_search"``，可被 enabled_toolsets / disabled_toolsets 过滤
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)


# =========================================================================
# Bridge 工具常量
# =========================================================================

BRIDGE_TOOLSET = "tool_search"
BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})


# =========================================================================
# Schema 定义（OpenAI function calling 格式）
# =========================================================================

TOOL_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tool_search",
        "description": (
            "按关键词搜索可用工具。返回匹配的 name + toolset + 一句话描述 + 入参概要。"
            "不返回完整 schema——拿到 name 后调用 tool_describe(name) 查完整定义。"
            "query 是自然语言或逗号分隔的关键词（例：'read file' / 'search, file'）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索词，匹配工具名/描述/工具集名（不区分大小写）",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回条数（默认 8，最大 30）",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_DESCRIBE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tool_describe",
        "description": (
            "获取指定工具的完整 OpenAI function schema（name + 描述 + 参数定义 + 必填项）。"
            "拿到 schema 后可直接用 tool_call(name, args) 执行，"
            "或在 tool_call_id 不可用时复制 schema 字段拼装原生 tool_call。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "工具名（必须与 tool_search 返回的 name 严格一致）",
                },
            },
            "required": ["name"],
        },
    },
}

TOOL_CALL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tool_call",
        "description": (
            "直接执行指定工具，参数以 JSON 对象形式传入。"
            "等价于原生 tool_calls——只是通过本桥接走，无需在请求里携带工具 schema。"
            "返回工具执行结果字符串（与原生调用一致）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "工具名（可用 tool_search 查可用名）",
                },
                "arguments": {
                    "type": "object",
                    "description": "工具参数（JSON 对象；字段以 tool_describe 返回的 schema 为准）",
                    "default": {},
                },
            },
            "required": ["name"],
        },
    },
}

ALL_BRIDGE_SCHEMAS = [TOOL_SEARCH_SCHEMA, TOOL_DESCRIBE_SCHEMA, TOOL_CALL_SCHEMA]


# =========================================================================
# Bridge handler（被 SpiritAgent.invoke_tool 截获调用，不走 registry.dispatch）
# =========================================================================

def _normalize_query(q: str) -> List[str]:
    """把搜索词拆成关键词（中英混合按非字母数字切）。"""
    if not q:
        return []
    # 拆成 token：中文按字、英文按 word
    tokens: List[str] = []
    for part in re.split(r"[\s,;|]+", q.strip()):
        if not part:
            continue
        # 英文片段按 word 拆，中文保留整体
        sub = re.findall(r"[A-Za-z_]+|[\u4e00-\u9fff]+", part)
        tokens.extend(sub) if sub else tokens.append(part)
    return [t.lower() for t in tokens if t]


def _score_entry(entry, tokens: List[str]) -> int:
    """对单个工具条目打分：name 完全命中 > name 包含 > toolset 命中 > 描述命中。"""
    name_l = entry.name.lower()
    desc_l = (entry.description or "").lower()
    toolset_l = entry.toolset.lower()
    score = 0
    for tok in tokens:
        if not tok:
            continue
        if tok == name_l:
            score += 10
        elif tok in name_l:
            score += 5
        if tok in toolset_l:
            score += 2
        if tok in desc_l:
            score += 1
    return score


def _summarize_parameters(schema: Dict[str, Any]) -> str:
    """从 OpenAI schema 里抽参数概要（name:type，必填标记）。"""
    fn = schema.get("function", {}) if isinstance(schema, dict) else {}
    params = fn.get("parameters", {}) or {}
    properties = params.get("properties", {}) or {}
    required = set(params.get("required", []) or [])
    if not properties:
        return "(no parameters)"
    parts = []
    for pname, pdef in properties.items():
        ptype = pdef.get("type", "any") if isinstance(pdef, dict) else "any"
        mark = "*" if pname in required else ""
        parts.append(f"{pname}{mark}:{ptype}")
    return ", ".join(parts)


def handle_tool_search(args: Dict[str, Any], *, agent: Any = None) -> str:
    """实现 ``tool_search(query, max_results)`` 桥接。"""
    query = (args.get("query") or "").strip()
    max_results = int(args.get("max_results") or 8)
    max_results = max(1, min(max_results, 30))
    tokens = _normalize_query(query)

    entries = registry.get_all_entries()
    # 排除 bridge 自身（避免自我引用）
    entries = [e for e in entries if e.name not in BRIDGE_TOOL_NAMES]

    if not tokens:
        # 没给关键词：按 toolset 分组列出前 N 个
        results = entries[:max_results]
    else:
        scored = [(e, _score_entry(e, tokens)) for e in entries]
        scored = [(e, s) for e, s in scored if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0].name))
        results = [e for e, _ in scored[:max_results]]

    if not results:
        return json.dumps({
            "query": query,
            "matched": 0,
            "hint": (
                f"没有工具匹配 '{query}'。试试更短的关键词，"
                "或调用 tool_describe(name) 直接看具体工具的 schema。"
            ),
        }, ensure_ascii=False)

    payload = {
        "query": query,
        "matched": len(results),
        "results": [
            {
                "name": e.name,
                "toolset": e.toolset,
                "description": (e.description or "")[:200],
                "parameters_summary": _summarize_parameters(e.schema),
            }
            for e in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def handle_tool_describe(args: Dict[str, Any], *, agent: Any = None) -> str:
    """实现 ``tool_describe(name)`` 桥接。"""
    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "缺少 name 参数"}, ensure_ascii=False)
    entry = registry.get_entry(name)
    if entry is None:
        # 模糊建议：取前 3 个最相似
        all_entries = registry.get_all_entries()
        scored = sorted(
            ((e, _score_entry(e, [name.lower()])) for e in all_entries),
            key=lambda x: -x[1],
        )[:3]
        suggestions = [e.name for e, s in scored if s > 0]
        return json.dumps({
            "error": f"工具 '{name}' 不存在",
            "suggestions": suggestions,
            "hint": "用 tool_search(query) 搜可用工具名",
        }, ensure_ascii=False)
    return json.dumps({
        "name": entry.name,
        "toolset": entry.toolset,
        "schema": entry.schema,
    }, ensure_ascii=False)


def handle_tool_call(args: Dict[str, Any], *, agent: Any = None) -> str:
    """实现 ``tool_call(name, arguments)`` 桥接（直接调 registry.dispatch）。"""
    name = (args.get("name") or "").strip()
    arguments = args.get("arguments") or {}
    if not name:
        return json.dumps({"error": "缺少 name 参数"}, ensure_ascii=False)
    if not isinstance(arguments, dict):
        return json.dumps({
            "error": "arguments 必须是 JSON 对象",
            "got_type": type(arguments).__name__,
        }, ensure_ascii=False)
    # 复用 Spirit 自己的 dispatch 路径（保留 check_fn / max_result / 签名探测等）
    return registry.dispatch(name, arguments)


# Bridge 名 → handler 映射
BRIDGE_HANDLERS = {
    "tool_search": handle_tool_search,
    "tool_describe": handle_tool_describe,
    "tool_call": handle_tool_call,
}


# =========================================================================
# Manifest 构造（注入 system prompt 的精简索引）
# =========================================================================

def _entry_to_manifest_line(entry) -> str:
    """单条 manifest 行：``- name (toolset): 描述``。"""
    desc = (entry.description or "").strip().replace("\n", " ")
    # 描述截到 120 字符，保留可读性
    if len(desc) > 120:
        desc = desc[:117] + "..."
    return f"- {entry.name} ({entry.toolset}): {desc}"


def build_tool_manifest(
    *,
    budget_pct: Optional[float] = None,
    context_length: Optional[int] = None,
    entries: Optional[List[Any]] = None,
) -> str:
    """生成精简 manifest 文本，按 token 预算降级。

    降级策略（按预算从宽到紧）：
    1. 装得下全部 → ``- name (toolset): 描述``
    2. 装不下全部 → 砍描述，只留 ``- name (toolset)``
    3. 还装不下 → 只留 ``- name``
    4. 极紧 → 只标 ``共 N 个工具，用 tool_search(query) 查``

    Args:
        budget_pct: manifest 占 context 比例（默认从 config 读 ``agent.tool_search.manifest_budget_pct``）
        context_length: context 总长度（默认读 ``llm.context_length``）
        entries: 工具条目列表（默认 registry.get_all_entries，排除 bridge 自身）

    Returns:
        manifest 文本（已含标题段、用法指引），可直接 ``\\n\\n`` 拼到 system prompt
    """
    if budget_pct is None:
        budget_pct = float(get_config_value("agent.tool_search.manifest_budget_pct", 5.0))
    if context_length is None:
        context_length = int(get_config_value("llm.context_length", 128_000))

    # 1 token ≈ 4 chars（粗估）
    chars_per_token = 4.0
    budget_chars = max(int(context_length * (budget_pct / 100.0) * chars_per_token), 800)

    if entries is None:
        entries = [e for e in registry.get_all_entries() if e.name not in BRIDGE_TOOL_NAMES]
    total = len(entries)

    header = (
        "## 可用工具索引（Tool Manifest）\n\n"
        f"本系统共有 {total} 个工具可通过本会话调用。"
        "你只能直接调用 3 个元工具（tool_search / tool_describe / tool_call），"
        "其它工具需要先通过 tool_search 找到名字，再用 tool_describe 查完整 schema，最后用 tool_call 执行。\n\n"
        "### 用法\n"
        "1. ``tool_search(query=\"读文件\")`` → 拿到候选 name 列表\n"
        "2. ``tool_describe(name=\"read_file\")`` → 拿到完整参数定义\n"
        "3. ``tool_call(name=\"read_file\", arguments={\"path\": \"...\"})`` → 执行\n\n"
        "### 工具清单\n"
    )

    # 三档降级
    full_lines = [_entry_to_manifest_line(e) for e in entries]
    full_text = header + "\n".join(full_lines)

    if len(full_text) <= budget_chars:
        return full_text

    # 砍描述
    short_lines = [f"- {e.name} ({e.toolset})" for e in entries]
    short_text = header + "\n".join(short_lines)
    if len(short_text) <= budget_chars:
        return short_text

    # 只留 name
    name_only_lines = [f"- {e.name}" for e in entries]
    name_only_text = header + "\n".join(name_only_lines)
    if len(name_only_text) <= budget_chars:
        return name_only_text

    # 极紧：只标总数 + 用法
    return (
        "## 可用工具索引（Tool Manifest）\n\n"
        f"本系统共有 {total} 个工具可通过本会话调用。"
        "本会话工具索引过长，未在 prompt 中展开。\n"
        "调用 ``tool_search(query=\"关键词\")`` 按需检索工具名，"
        "再 ``tool_describe(name)`` 查完整参数定义，"
        "最后 ``tool_call(name, arguments)`` 执行。\n"
    )


# =========================================================================
# 自注册（让 registry.get_definitions 自然包含 3 个 bridge）
# =========================================================================

def _bridge_check_fn() -> bool:
    """Bridge 工具永远可用。"""
    return True


for _schema, _name, _handler in [
    (TOOL_SEARCH_SCHEMA, "tool_search", handle_tool_search),
    (TOOL_DESCRIBE_SCHEMA, "tool_describe", handle_tool_describe),
    (TOOL_CALL_SCHEMA, "tool_call", handle_tool_call),
]:
    # handler 签名是 (args, *, agent=None)，不是标准 (args) 也不是 (**args)；
    # 直接走专用 bridge dispatcher 路径，注册到 registry 时仍提供 handler
    # （如果被直接 dispatch 也能用，handler 忽略多余 keyword）。
    def _make_handler(h=_handler):
        def _wrapped(args: Dict[str, Any]) -> str:
            return h(args)
        return _wrapped
    registry.register(
        name=_name,
        toolset=BRIDGE_TOOLSET,
        schema=_schema,
        handler=_make_handler(),
        check_fn=_bridge_check_fn,
        description=_schema["function"]["description"],
        emoji="🔍" if _name == "tool_search" else ("📋" if _name == "tool_describe" else "▶️"),
    )


__all__ = [
    "BRIDGE_TOOLSET",
    "BRIDGE_TOOL_NAMES",
    "BRIDGE_HANDLERS",
    "TOOL_SEARCH_SCHEMA",
    "TOOL_DESCRIBE_SCHEMA",
    "TOOL_CALL_SCHEMA",
    "ALL_BRIDGE_SCHEMAS",
    "handle_tool_search",
    "handle_tool_describe",
    "handle_tool_call",
    "build_tool_manifest",
]
