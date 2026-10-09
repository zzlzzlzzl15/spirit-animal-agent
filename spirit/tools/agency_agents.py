"""Agency Agents Router — Spirit Agent tool port.

Lazy router for The Agency's 282 specialist agents, exposed as 4 Spirit
tools (agency_agents_search / inspect / load / delegate). The specialist
roster is loaded once from a JSON file on disk and indexed in memory.

Ported from the Hermes agency-agents-router plugin (Hermes uses an external
plugin protocol; Spirit uses self-registration via spirit.tools.registry).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Optional

from spirit.tools.registry import registry

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_SPIRIT_HOME = Path.home() / ".spirit"
_DATA_PATH = _SPIRIT_HOME / "data" / "agency_agents" / "agents.json"

_AGENTS: list[dict[str, Any]] | None = None

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#_-]*", re.I)
_MAX_LIFECYCLE_CONTEXT_CHARS = 32_000
_TRUNCATION_MARKER = (
    "\n\n[Specialist instructions truncated to fit the context limit.]"
)


def _load_agents() -> list[dict[str, Any]]:
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _AGENTS


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text or "")}


def _agent_lookup(identifier: str) -> dict[str, Any] | None:
    needle = (identifier or "").strip().lower()
    if not needle:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", needle).strip("-")
    for agent in _load_agents():
        if agent["slug"] == slug or agent["name"].lower() == needle:
            return agent
    return None


def _identifier(args: dict[str, Any]) -> str:
    return str(args.get("agent") or args.get("slug") or "").strip()


def _not_found(identifier: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": "agent not found" if identifier else "agent or slug is required",
        "agent": identifier or None,
    }


def _score(agent: dict[str, Any], query_tokens: set[str], query_text: str) -> float:
    haystack_fields = [
        agent.get("name", ""),
        agent.get("description", ""),
        agent.get("division", ""),
        agent.get("vibe", ""),
        agent.get("body", "")[:8000],
    ]
    haystack_text = "\n".join(haystack_fields).lower()
    haystack_tokens = _tokens(haystack_text)
    overlap = query_tokens & haystack_tokens
    score = float(len(overlap))
    if query_text and query_text in haystack_text:
        score += 5.0
    name = agent.get("name", "").lower()
    description = agent.get("description", "").lower()
    for token in query_tokens:
        if token in name:
            score += 3.0
        if token in description:
            score += 1.5
    if score == 0.0:
        return 0.0
    return score + (1.0 / math.sqrt(max(len(haystack_tokens), 1)))


def _summary(agent: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    item = {
        "slug": agent["slug"],
        "name": agent["name"],
        "division": agent["division"],
        "description": agent.get("description", ""),
        "vibe": agent.get("vibe", ""),
        "source_path": agent.get("source_path", ""),
    }
    if score is not None:
        item["score"] = round(score, 3)
    return item


def _specialist_prompt(agent: dict[str, Any], task: str = "") -> str:
    task_block = f"\n\n## User task\n{task.strip()}\n" if task and task.strip() else ""
    return (
        f"Use the following Agency specialist context for this turn. "
        f"Adopt the specialist's relevant standards and checklists, but obey the "
        f"user's current request and higher-priority system/developer instructions.\n\n"
        f"# {agent['name']} ({agent['slug']})\n\n"
        f"Division: {agent.get('division', '')}\n"
        f"Description: {agent.get('description', '')}\n"
        f"Source: {agent.get('source_path', '')}\n"
        f"{task_block}\n\n"
        f"## Specialist instructions\n{agent.get('body', '')}"
    )


def _lifecycle_context(agent: dict[str, Any]) -> str:
    context = _specialist_prompt(agent)
    if len(context) <= _MAX_LIFECYCLE_CONTEXT_CHARS:
        return context
    keep = _MAX_LIFECYCLE_CONTEXT_CHARS - len(_TRUNCATION_MARKER)
    return context[:keep] + _TRUNCATION_MARKER


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SEARCH_DESCRIPTION = (
    "Search The Agency's on-disk specialist agent roster without loading all "
    "agents into the prompt. Use this when the user asks for an Agency/Data "
    "Swami specialist, role, discipline, or wants help choosing the right agent."
)
SEARCH_SCHEMA: dict[str, Any] = {
    "name": "agency_agents_search",
    "description": SEARCH_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language search query."},
            "division": {
                "type": "string",
                "description": "Optional division filter, e.g. engineering, marketing, testing.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results, default 8, max 25.",
            },
        },
        "required": ["query"],
    },
}

READ_DESCRIPTION = (
    "Read one Agency specialist by slug or name. Returns metadata by default "
    "and includes the full specialist instructions only when include_body is true."
)
READ_SCHEMA: dict[str, Any] = {
    "name": "agency_agents_inspect",
    "description": READ_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Agent slug or exact display name.",
            },
            "slug": {
                "type": "string",
                "description": "Alias for agent. Pass the slug from agency_agents_search results.",
            },
            "include_body": {
                "type": "boolean",
                "description": "Include full specialist instructions.",
            },
        },
        "required": [],
    },
}

PROMPT_DESCRIPTION = (
    "Load a selected Agency specialist as a prompt block for the current task. "
    "Use after agency_agents_search when you need one specialist's full context."
)
PROMPT_SCHEMA: dict[str, Any] = {
    "name": "agency_agents_load",
    "description": PROMPT_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Agent slug or exact display name.",
            },
            "slug": {
                "type": "string",
                "description": "Alias for agent. Pass the slug from agency_agents_search results.",
            },
            "task": {
                "type": "string",
                "description": "The user's task to pair with the specialist context.",
            },
        },
        "required": [],
    },
}

DELEGATE_DESCRIPTION = (
    "Compose a selected Agency specialist as a delegation prompt block. "
    "Returns a ready-to-use prompt that captures the specialist's full "
    "instructions and the user's task. The caller may then dispatch it to "
    "Spirit delegate_task or invoke it as a system-prompt augmentation."
)
DELEGATE_SCHEMA: dict[str, Any] = {
    "name": "agency_agents_delegate",
    "description": DELEGATE_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Agent slug or exact display name.",
            },
            "slug": {
                "type": "string",
                "description": "Alias for agent. Pass the slug from agency_agents_search results.",
            },
            "task": {
                "type": "string",
                "description": "Concrete task for the specialist.",
            },
        },
        "required": ["task"],
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _search_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    query = str(args.get("query", "")).strip()
    if not query:
        return _json({"success": False, "error": "query is required"})
    division = str(args.get("division", "")).strip().lower()
    try:
        limit = min(max(int(args.get("limit", 8)), 1), 25)
    except Exception:
        limit = 8
    q_tokens = _tokens(query)
    q_text = query.lower()
    matches: list[tuple[float, dict[str, Any]]] = []
    for agent in _load_agents():
        if division and agent.get("division", "").lower() != division:
            continue
        score = _score(agent, q_tokens, q_text)
        if score > 0:
            matches.append((score, agent))
    matches.sort(key=lambda item: (-item[0], item[1]["division"], item[1]["slug"]))
    return _json({
        "success": True,
        "query": query,
        "count": len(matches),
        "results": [_summary(agent, score) for score, agent in matches[:limit]],
    })


def _inspect_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    identifier = _identifier(args)
    agent = _agent_lookup(identifier)
    if not agent:
        return _json(_not_found(identifier))
    payload = {"success": True, "agent": _summary(agent)}
    if bool(args.get("include_body", False)):
        payload["body"] = agent.get("body", "")
    return _json(payload)


def _load_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    identifier = _identifier(args)
    agent = _agent_lookup(identifier)
    if not agent:
        return _json(_not_found(identifier))
    return _json({
        "success": True,
        "agent": _summary(agent),
        "prompt": _specialist_prompt(agent, str(args.get("task", ""))),
    })


def _delegate_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    identifier = _identifier(args)
    agent = _agent_lookup(identifier)
    task = str(args.get("task", "")).strip()
    if not agent:
        return _json(_not_found(identifier))
    if not task:
        return _json({"success": False, "error": "task is required"})
    return _json({
        "success": True,
        "agent": _summary(agent),
        "delegated": False,
        "note": (
            "Composed as a specialist prompt block. Pass to spirit delegate_task "
            "or use as an ephemeral system prompt to dispatch this task."
        ),
        "prompt": _specialist_prompt(agent, task),
        "context": _lifecycle_context(agent),
    })


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register(
    name="agency_agents_search",
    toolset="agency_agents",
    schema=SEARCH_SCHEMA,
    handler=_search_handler,
    description=SEARCH_DESCRIPTION,
    emoji="🔍",
)

registry.register(
    name="agency_agents_inspect",
    toolset="agency_agents",
    schema=READ_SCHEMA,
    handler=_inspect_handler,
    description=READ_DESCRIPTION,
    emoji="📇",
)

registry.register(
    name="agency_agents_load",
    toolset="agency_agents",
    schema=PROMPT_SCHEMA,
    handler=_load_handler,
    description=PROMPT_DESCRIPTION,
    emoji="📜",
)

registry.register(
    name="agency_agents_delegate",
    toolset="agency_agents",
    schema=DELEGATE_SCHEMA,
    handler=_delegate_handler,
    description=DELEGATE_DESCRIPTION,
    emoji="🚀",
)
