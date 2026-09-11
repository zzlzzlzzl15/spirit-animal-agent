"""平台集成工具 — Spirit Agent。

合并自 Hermes:
- discord_tool.py: Discord 服务器交互
- feishu_doc_tool.py: 飞书文档读取
- feishu_drive_tool.py: 飞书云文档评论
- homeassistant_tool.py: Home Assistant 智能家居
- send_message_tool.py: 跨平台消息发送
- x_search_tool.py: X/Twitter 搜索
- yuanbao_tools.py: 元宝平台工具集
"""

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# Discord 工具
# ============================================================================

DISCORD_API_BASE = "https://discord.com/api/v10"


class DiscordAPIError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Discord API error {status}: {body}")


def _get_bot_token() -> Optional[str]:
    return os.getenv("DISCORD_BOT_TOKEN", "").strip() or None


def _discord_request(method: str, path: str, token: str,
                     params: Optional[Dict] = None, body: Optional[Dict] = None,
                     timeout: int = 15) -> Any:
    url = f"{DISCORD_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read(64 * 1024).decode("utf-8", errors="replace")
        raise DiscordAPIError(e.code, error_body)


DISCORD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord",
        "description": "Discord 服务器交互：列出服务器/频道/成员、获取消息等。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_guilds", "list_channels", "list_members",
                             "fetch_messages", "get_member", "search_members"],
                    "description": "操作类型",
                },
                "guild_id": {"type": "string", "description": "服务器 ID"},
                "channel_id": {"type": "string", "description": "频道 ID"},
                "member_id": {"type": "string", "description": "成员 ID"},
                "limit": {"type": "integer", "description": "返回数量限制", "default": 50},
                "query": {"type": "string", "description": "搜索查询"},
            },
            "required": ["action"],
        },
    },
}


def _handle_discord(args: Dict[str, Any], **kwargs) -> str:
    token = _get_bot_token()
    if not token:
        return json.dumps({"error": "DISCORD_BOT_TOKEN 未设置"})
    action = args.get("action", "")
    guild_id = args.get("guild_id", "")
    channel_id = args.get("channel_id", "")
    limit = args.get("limit", 50)
    try:
        if action == "list_guilds":
            result = _discord_request("GET", "/users/@me/guilds", token)
            return json.dumps({"success": True, "guilds": result[:limit]})
        elif action == "list_channels":
            if not guild_id:
                return json.dumps({"error": "guild_id 必填"})
            result = _discord_request("GET", f"/guilds/{guild_id}/channels", token)
            return json.dumps({"success": True, "channels": result[:limit]})
        elif action == "list_members":
            if not guild_id:
                return json.dumps({"error": "guild_id 必填"})
            result = _discord_request("GET", f"/guilds/{guild_id}/members", token,
                                      params={"limit": str(min(limit, 1000))})
            return json.dumps({"success": True, "members": result})
        elif action == "fetch_messages":
            if not channel_id:
                return json.dumps({"error": "channel_id 必填"})
            result = _discord_request("GET", f"/channels/{channel_id}/messages", token,
                                      params={"limit": str(min(limit, 100))})
            return json.dumps({"success": True, "messages": result})
        elif action == "get_member":
            if not guild_id or not args.get("member_id"):
                return json.dumps({"error": "guild_id 和 member_id 必填"})
            result = _discord_request("GET", f"/guilds/{guild_id}/members/{args['member_id']}", token)
            return json.dumps({"success": True, "member": result})
        elif action == "search_members":
            if not guild_id:
                return json.dumps({"error": "guild_id 必填"})
            query = args.get("query", "")
            result = _discord_request("GET", f"/guilds/{guild_id}/members/search", token,
                                      params={"query": query, "limit": str(min(limit, 1000))})
            return json.dumps({"success": True, "members": result})
        else:
            return json.dumps({"error": f"未知操作: {action}"})
    except DiscordAPIError as e:
        return json.dumps({"error": f"Discord API 错误 ({e.status}): {e.body[:300]}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def check_discord() -> bool:
    return bool(_get_bot_token())


registry.register(
    name="discord", toolset="platforms", schema=DISCORD_SCHEMA,
    handler=_handle_discord, check_fn=check_discord, emoji="💬",
)


# ============================================================================
# Home Assistant 智能家居工具
# ============================================================================

from spirit.config import get_config_value

_HASS_URL_DEFAULT = get_config_value("integrations.homeassistant_url", "http://homeassistant.local:8123")
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BLOCKED_DOMAINS = frozenset({
    "shell_command", "command_line", "python_script",
    "pyscript", "hassio", "rest_command",
})


def _get_ha_config():
    url = os.getenv("HASS_URL", _HASS_URL_DEFAULT).rstrip("/")
    token = os.getenv("HASS_TOKEN", "")
    return url, token


def _ha_request(method: str, path: str, token: str, base_url: str,
                body: Optional[Dict] = None, timeout: int = 15) -> Any:
    import requests as req_lib
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = req_lib.request(method, url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json() if resp.text else None


HA_LIST_ENTITIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ha_list_entities",
        "description": "列出/筛选 Home Assistant 实体（按 domain 或 area）。",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "实体域（如 light, sensor）"},
                "area": {"type": "string", "description": "区域名称"},
            },
        },
    },
}


def _handle_ha_list_entities(args: Dict[str, Any], **kwargs) -> str:
    base_url, token = _get_ha_config()
    if not token:
        return json.dumps({"error": "HASS_TOKEN 未设置"})
    domain = args.get("domain", "")
    area = args.get("area", "")
    try:
        states = _ha_request("GET", "/api/states", token, base_url)
        filtered = states
        if domain:
            filtered = [s for s in filtered if s.get("entity_id", "").startswith(f"{domain}.")]
        if area:
            area_lower = area.lower()
            filtered = [s for s in filtered if area_lower in
                        (s.get("attributes", {}).get("friendly_name", "") or "").lower()]
        entities = [{"entity_id": s["entity_id"], "state": s["state"],
                      "friendly_name": s.get("attributes", {}).get("friendly_name", "")}
                     for s in filtered]
        return json.dumps({"success": True, "count": len(entities), "entities": entities[:200]})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


HA_GET_STATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ha_get_state",
        "description": "获取 Home Assistant 单个实体的详细状态。",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string", "description": "实体 ID"}},
            "required": ["entity_id"],
        },
    },
}


def _handle_ha_get_state(args: Dict[str, Any], **kwargs) -> str:
    base_url, token = _get_ha_config()
    if not token:
        return json.dumps({"error": "HASS_TOKEN 未设置"})
    entity_id = args.get("entity_id", "")
    if not entity_id or not _ENTITY_ID_RE.match(entity_id):
        return json.dumps({"error": f"无效的 entity_id: {entity_id}"})
    try:
        result = _ha_request("GET", f"/api/states/{entity_id}", token, base_url)
        return json.dumps({"success": True, "entity": result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


HA_CALL_SERVICE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ha_call_service",
        "description": "调用 Home Assistant 服务（如 turn_on, turn_off, set_temperature）。",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "服务域（如 light, switch）"},
                "service": {"type": "string", "description": "服务名（如 turn_on）"},
                "entity_id": {"type": "string", "description": "目标实体 ID"},
                "data": {"type": "object", "description": "额外参数"},
            },
            "required": ["domain", "service"],
        },
    },
}


def _handle_ha_call_service(args: Dict[str, Any], **kwargs) -> str:
    base_url, token = _get_ha_config()
    if not token:
        return json.dumps({"error": "HASS_TOKEN 未设置"})
    domain = args.get("domain", "")
    service = args.get("service", "")
    if not _SERVICE_NAME_RE.match(domain) or not _SERVICE_NAME_RE.match(service):
        return json.dumps({"error": "无效的 domain 或 service 名称"})
    if domain in _BLOCKED_DOMAINS:
        return json.dumps({"error": f"安全策略阻止了 {domain} 域的服务调用"})
    entity_id = args.get("entity_id", "")
    data = args.get("data", {})
    body = {**data}
    if entity_id:
        body["entity_id"] = entity_id
    try:
        result = _ha_request("POST", f"/api/services/{domain}/{service}", token, base_url, body=body)
        return json.dumps({"success": True, "result": result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def check_ha() -> bool:
    _, token = _get_ha_config()
    return bool(token)


registry.register(name="ha_list_entities", toolset="platforms", schema=HA_LIST_ENTITIES_SCHEMA,
                  handler=_handle_ha_list_entities, check_fn=check_ha, emoji="🏠")
registry.register(name="ha_get_state", toolset="platforms", schema=HA_GET_STATE_SCHEMA,
                  handler=_handle_ha_get_state, check_fn=check_ha, emoji="🏠")
registry.register(name="ha_call_service", toolset="platforms", schema=HA_CALL_SERVICE_SCHEMA,
                  handler=_handle_ha_call_service, check_fn=check_ha, emoji="🏠")


# ============================================================================
# 飞书文档工具
# ============================================================================

FEISHU_DOC_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "feishu_doc_read",
        "description": "读取飞书/Lark 文档的纯文本内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_token": {"type": "string", "description": "文档 token"},
            },
            "required": ["doc_token"],
        },
    },
}


def _handle_feishu_doc_read(args: Dict[str, Any], **kwargs) -> str:
    return json.dumps({
        "success": False,
        "error": "飞书文档读取需要 lark_oapi SDK 和飞书应用上下文。",
    })


def check_feishu() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


registry.register(name="feishu_doc_read", toolset="platforms", schema=FEISHU_DOC_READ_SCHEMA,
                  handler=_handle_feishu_doc_read, check_fn=check_feishu, emoji="📄")


# ============================================================================
# X/Twitter 搜索工具
# ============================================================================

X_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "x_search",
        "description": (
            "搜索 X/Twitter 上的内容。使用 xAI API 进行带引用的搜索。\n"
            "需要 XAI_API_KEY 或 xAI Grok OAuth 认证。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "from_date": {"type": "string", "description": "起始日期 (YYYY-MM-DD)"},
                "to_date": {"type": "string", "description": "结束日期 (YYYY-MM-DD)"},
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "限定搜索的账号列表",
                },
                "max_results": {"type": "integer", "description": "最大结果数", "default": 10},
            },
            "required": ["query"],
        },
    },
}


def _handle_x_search(args: Dict[str, Any], **kwargs) -> str:
    api_key = os.getenv("XAI_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "XAI_API_KEY 未设置"})
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "query 不能为空"})

    try:
        import requests as req_lib
        xai_base = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
        model = os.getenv("X_SEARCH_MODEL", "grok-4.20-reasoning")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "tools": [{"type": "x_search"}],
        }
        response = req_lib.post(
            f"{xai_base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=180,
        )
        if response.status_code != 200:
            return json.dumps({"error": f"xAI API 错误: {response.status_code}"})
        result = response.json()
        return json.dumps({
            "success": True,
            "answer": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
            "citations": result.get("citations", []),
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def check_x_search() -> bool:
    return bool(os.getenv("XAI_API_KEY"))


registry.register(name="x_search", toolset="platforms", schema=X_SEARCH_SCHEMA,
                  handler=_handle_x_search, check_fn=check_x_search, emoji="🐦")


# ============================================================================
# 跨平台消息发送工具
# ============================================================================

SEND_MESSAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": (
            "向已连接的消息平台发送消息。支持 Telegram、Discord、Slack 等。\n"
            "需要提供目标（用户/频道 ID）和消息内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "目标（用户 ID 或频道 ID）"},
                "message": {"type": "string", "description": "消息内容"},
                "platform": {
                    "type": "string",
                    "description": "平台名称（可选，自动检测当前活跃平台）",
                },
                "media": {
                    "type": "string",
                    "description": "附件路径（图片/视频/文档）",
                },
            },
            "required": ["to", "message"],
        },
    },
}


def _handle_send_message(args: Dict[str, Any], **kwargs) -> str:
    to = args.get("to", "")
    message = args.get("message", "")
    platform = args.get("platform", "")
    if not to or not message:
        return json.dumps({"error": "to 和 message 必填"})

    # Telegram
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if (not platform or platform == "telegram") and tg_token:
        try:
            import requests as req_lib
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            resp = req_lib.post(url, json={"chat_id": to, "text": message}, timeout=15)
            if resp.status_code == 200:
                return json.dumps({"success": True, "platform": "telegram"})
            return json.dumps({"error": f"Telegram 错误: {resp.status_code}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # Discord
    disc_token = _get_bot_token()
    if (not platform or platform == "discord") and disc_token:
        try:
            _discord_request("POST", f"/channels/{to}/messages", disc_token,
                             body={"content": message})
            return json.dumps({"success": True, "platform": "discord"})
        except Exception as exc:
            return json.dumps({"error": f"Discord: {exc}"})

    return json.dumps({"error": "没有可用的消息平台。设置 TELEGRAM_BOT_TOKEN 或 DISCORD_BOT_TOKEN。"})


def check_send_message() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") or _get_bot_token())


registry.register(name="send_message", toolset="platforms", schema=SEND_MESSAGE_SCHEMA,
                  handler=_handle_send_message, check_fn=check_send_message, emoji="📨")


# ============================================================================
# 元宝平台工具（占位）
# ============================================================================

YUANBAO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "yuanbao",
        "description": "元宝平台交互：查询群信息、群成员、发送贴纸等。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_group_info", "query_members", "search_sticker", "send_sticker", "send_dm"],
                    "description": "操作类型",
                },
                "group_code": {"type": "string", "description": "群编号"},
                "name": {"type": "string", "description": "搜索关键词/用户名"},
            },
            "required": ["action"],
        },
    },
}


def _handle_yuanbao(args: Dict[str, Any], **kwargs) -> str:
    return json.dumps({
        "success": False,
        "error": "元宝平台需要 gateway 连接上下文，当前不在元宝平台环境中。",
    })


registry.register(name="yuanbao", toolset="platforms", schema=YUANBAO_SCHEMA,
                  handler=_handle_yuanbao, check_fn=lambda: False, emoji="🪙")
