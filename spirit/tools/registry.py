"""工具注册表 — Spirit Agent 的工具调度中心。

参考 Hermes Agent 的 tools/registry.py 设计，实现自注册模式的工具管理。

导入链（循环安全）：
    spirit/tools/registry.py  ← 无外部依赖
           ↑
    spirit/tools/*.py         ← 每个工具文件导入时自注册
           ↑
    spirit/agent/agent.py     ← 查询注册表获取工具定义
"""

import ast
import importlib
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具条目
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    """单个已注册工具的元数据。"""

    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Optional[Callable] = None
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    max_result_size_chars: Optional[int] = None
    # handler 签名约定：True = args-dict 形式 handler(args: Dict, **kwargs)；
    # False = 显式 kwargs 形式 handler(**args)。注册时自动探测。
    takes_args_dict: bool = False


# ---------------------------------------------------------------------------
# check_fn TTL 缓存
# ---------------------------------------------------------------------------

from spirit.config import get_config_value

_CHECK_FN_TTL = get_config_value("internal.check_fn_ttl", 30.0)  # 缓存 30 秒
_check_fn_cache: Dict[Callable, tuple] = {}
_check_fn_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    """带 TTL 缓存的 check_fn 调用，避免频繁探测外部状态。"""
    now = time.monotonic()
    with _check_fn_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None:
            ts, value = cached
            if now - ts < _CHECK_FN_TTL:
                return value

    try:
        value = bool(fn())
    except Exception:
        value = False

    with _check_fn_lock:
        _check_fn_cache[fn] = (now, value)
        return value


def invalidate_check_fn_cache() -> None:
    """清除所有 check_fn 缓存。配置变更后调用。"""
    with _check_fn_lock:
        _check_fn_cache.clear()


# ---------------------------------------------------------------------------
# 工具发现
# ---------------------------------------------------------------------------

def _module_registers_tools(module_path: Path) -> bool:
    """检查模块文件是否包含顶层 registry.register() 调用。"""
    try:
        source = module_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if "registry" not in source or "register" not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        return False

    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "register"
            and isinstance(func.value, ast.Name)
            and func.value.id == "registry"
        ):
            return True
    return False


def discover_tools(tools_dir: Optional[Path] = None) -> List[str]:
    """扫描工具目录，导入所有自注册的工具模块。"""
    tools_path = Path(tools_dir) if tools_dir else Path(__file__).resolve().parent
    module_names = [
        f"spirit.tools.{path.stem}"
        for path in sorted(tools_path.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py"}
        and _module_registers_tools(path)
    ]

    imported: List[str] = []
    for mod_name in module_names:
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as e:
            logger.warning("无法导入工具模块 %s: %s", mod_name, e)
    return imported


# ---------------------------------------------------------------------------
# handler 签名约定探测
# ---------------------------------------------------------------------------

def _detect_args_dict_handler(handler: Callable) -> bool:
    """探测 handler 是否采用 args-dict 签名约定。

    Spirit 工具 handler 有两种历史约定：
    1. args-dict：``def _handle_x(args: Dict[str, Any], **kwargs)``
       —— 首个位置参数名为 ``args``，期望收到整个参数字典。
    2. 显式 kwargs：``def _handle_x(path: str, offset: int = 0)``
       —— 期望 ``handler(**args)`` 展开调用。

    ``dispatch`` 统一用 ``handler(**args)`` 会让约定 1 因缺少位置参数
    ``args`` 而 TypeError。此函数在注册时探测约定，让 dispatch 能正确适配两者。

    探测失败（内置函数 / 无签名）时保守返回 False（按显式 kwargs 处理）。
    """
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return False
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            # 首个位置参数名为 args → args-dict 约定
            return param.name == "args"
        # 首个参数就是 *args / **kwargs / keyword-only → 非 args-dict
        break
    return False


# ---------------------------------------------------------------------------
# 工具注册表（全局单例）
# ---------------------------------------------------------------------------

class ToolRegistry:
    """工具注册表 — 管理所有工具的注册、查询和分发。

    设计参考 Hermes 的 ToolRegistry，核心特性：
    - 自注册模式：工具文件导入时自动调用 register()
    - check_fn TTL 缓存：避免频繁探测外部依赖
    - 线程安全：所有操作加锁
    - 代际计数：每次变更递增，支持缓存失效检测
    """

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    @property
    def generation(self) -> int:
        """当前代际号，每次 register/deregister 递增。"""
        return self._generation

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: Optional[int] = None,
    ) -> None:
        """注册一个工具。由工具文件在模块级别调用。

        Args:
            name: 工具名称（如 "read_file"）
            toolset: 所属工具集（如 "file"）
            schema: OpenAI function calling 格式的 schema
            handler: 实际处理函数
            check_fn: 可用性检查函数（可选）
            is_async: 是否为异步处理函数
            description: 工具描述
            emoji: 工具图标
            max_result_size_chars: 结果最大字符数
        """
        with self._lock:
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                is_async=is_async,
                description=description or schema.get("function", {}).get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
                takes_args_dict=_detect_args_dict_handler(handler),
            )
            self._generation += 1
            logger.debug("注册工具: %s (toolset=%s)", name, toolset)

    def deregister(self, name: str) -> None:
        """注销一个工具。"""
        with self._lock:
            if self._tools.pop(name, None):
                self._generation += 1

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """按名称获取工具条目。"""
        with self._lock:
            return self._tools.get(name)

    def get_all_entries(self) -> List[ToolEntry]:
        """获取所有工具条目的快照。"""
        with self._lock:
            return list(self._tools.values())

    def get_tool_names(self) -> List[str]:
        """获取所有已注册工具名称。"""
        with self._lock:
            return sorted(self._tools.keys())

    def get_toolset_names(self) -> List[str]:
        """获取所有已注册的工具集名称。"""
        with self._lock:
            return sorted({e.toolset for e in self._tools.values()})

    # ------------------------------------------------------------------
    # 工具定义生成（OpenAI function calling 格式）
    # ------------------------------------------------------------------

    def get_definitions(
        self,
        enabled_toolsets: Optional[List[str]] = None,
        disabled_toolsets: Optional[List[str]] = None,
    ) -> List[dict]:
        """生成 OpenAI function calling 格式的工具定义列表。

        Args:
            enabled_toolsets: 仅包含这些工具集（None = 全部）
            disabled_toolsets: 排除这些工具集

        Returns:
            OpenAI tools 参数格式的 schema 列表
        """
        enabled = set(enabled_toolsets) if enabled_toolsets else None
        disabled = set(disabled_toolsets) if disabled_toolsets else set()

        definitions = []
        for entry in self._snapshot_entries():
            # 工具集过滤
            if enabled is not None and entry.toolset not in enabled:
                continue
            if entry.toolset in disabled:
                continue

            # check_fn 可用性检查
            if entry.check_fn and not _check_fn_cached(entry.check_fn):
                continue

            # 构建 OpenAI 格式
            schema = entry.schema
            if schema.get("type") != "function":
                schema = {
                    "type": "function",
                    "function": {
                        "name": entry.name,
                        "description": entry.description,
                        "parameters": schema,
                    },
                }
            definitions.append(schema)

        return definitions

    def _snapshot_entries(self) -> List[ToolEntry]:
        """获取工具条目的线程安全快照。"""
        with self._lock:
            return list(self._tools.values())

    # ------------------------------------------------------------------
    # 工具调用分发
    # ------------------------------------------------------------------

    def dispatch(self, name: str, args: Dict[str, Any]) -> str:
        """分发工具调用到对应的处理函数。

        Args:
            name: 工具名称
            args: 工具参数

        Returns:
            工具执行结果（字符串）
        """
        entry = self.get_entry(name)
        if entry is None:
            return json.dumps({"error": f"工具 '{name}' 不存在"})

        try:
            # 适配两种 handler 签名约定（args-dict vs 显式 kwargs）
            if entry.takes_args_dict:
                result = entry.handler(args)
            else:
                result = entry.handler(**args)
            # 截断过大的结果
            if entry.max_result_size_chars and isinstance(result, str):
                if len(result) > entry.max_result_size_chars:
                    result = (
                        result[: entry.max_result_size_chars]
                        + f"\n\n... [结果已截断，共 {len(result)} 字符]"
                    )
            return result if isinstance(result, str) else json.dumps(result)
        except TypeError as e:
            # 参数签名不匹配（常见于模型输出畸形导致参数丢失/多余）：
            # 返回带完整参数清单的结构化提示，引导模型自行重试
            logger.warning("工具 %s 参数错误: %s", name, e)
            hint = self._signature_hint(entry)
            return json.dumps({
                "error": f"工具 '{name}' 参数不正确: {e}",
                "hint": hint,
            }, ensure_ascii=False)
        except Exception as e:
            logger.warning("工具 %s 执行失败: %s", name, e)
            return json.dumps({"error": f"工具执行失败: {e}"})

    @staticmethod
    def _signature_hint(entry: ToolEntry) -> str:
        """生成 handler 参数清单提示，供参数错误时引导模型重试。"""
        try:
            import inspect

            sig = inspect.signature(entry.handler)
            params = []
            for pname, p in sig.parameters.items():
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                if p.default is inspect.Parameter.empty:
                    params.append(f"{pname}（必填）")
                else:
                    params.append(f"{pname}（可选，默认 {p.default!r}）")
            if params:
                return (
                    f"请重新调用工具 '{entry.name}'，在 arguments JSON 中完整提供以下参数："
                    + "、".join(params)
                )
        except (TypeError, ValueError):
            pass
        return f"请重新调用工具 '{entry.name}'，并在 arguments JSON 中提供全部必填参数"


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

registry = ToolRegistry()
