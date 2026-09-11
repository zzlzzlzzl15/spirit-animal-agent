"""工具调用护栏 — 防止 LLM 陷入重复失败循环。

参考 Hermes 的 tool_guardrails.py (480行)。
纯函数控制器：追踪每轮工具调用观察，返回决策。
运行时代码决定这些决策是变成警告指导、合成工具结果还是受控停止。

核心功能：
- 完全相同参数重复失败检测 → 警告/阻止
- 同一工具多次失败检测 → 警告/停止
- 幂等工具无进展检测 → 警告/阻止
- 可配置阈值（从 config 加载）
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 工具分类
# =========================================================================

# 幂等工具（只读，重复调用无意义）
IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "web_search", "web_extract",
    "web_fetch", "session_search",
    "browser_snapshot", "browser_console", "browser_get_images",
    "list_dir", "file_info", "search_files_content", "find_files",
    "git_status", "git_diff", "git_log", "project_info",
    "image_analysis", "read_extract",
})

# 变异工具（有副作用）
MUTATING_TOOL_NAMES = frozenset({
    "terminal", "execute_code", "write_file", "patch_file",
    "create_file", "delete_file", "move_file", "create_directory",
    "todo", "memory", "checkpoint",
    "browser_click", "browser_type", "browser_press",
    "browser_scroll", "browser_navigate",
    "send_message", "cronjob", "delegate_task",
    "image_generation", "video_generation",
})


# =========================================================================
# 配置
# =========================================================================

@dataclass(frozen=True)
class ToolCallGuardrailConfig:
    """每轮工具调用循环检测阈值。

    警告默认启用，不阻止工具执行。
    硬停止为显式 opt-in（config.yaml 配置）。
    """

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False

    # 完全相同参数失败
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5

    # 同一工具失败（不同参数）
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8

    # 幂等工具无进展（相同结果）
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5

    idempotent_tools: frozenset = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset = field(default_factory=lambda: MUTATING_TOOL_NAMES)

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "ToolCallGuardrailConfig":
        """从 config dict 构建。"""
        if not isinstance(data, Mapping):
            return cls()

        defaults = cls()
        warn_after = data.get("warn_after", {}) or {}
        hard_stop_after = data.get("hard_stop_after", {}) or {}

        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
        )


# =========================================================================
# 数据结构
# =========================================================================

@dataclass(frozen=True)
class ToolCallSignature:
    """工具名 + 参数哈希的稳定身份标识。"""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Optional[Mapping[str, Any]]) -> "ToolCallSignature":
        canonical = canonical_tool_args(args or {})
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """护栏控制器的决策结果。"""

    action: str = "allow"   # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: Optional[ToolCallSignature] = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}


# =========================================================================
# 辅助函数
# =========================================================================

def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """返回排序后的紧凑 JSON。"""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(
        args, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def classify_tool_failure(tool_name: str, result: Optional[str]) -> tuple[bool, str]:
    """从工具结果判断是否失败。

    Returns:
        (is_failed, detail_suffix)
    """
    if result is None:
        return False, ""

    # terminal 工具看 exit_code
    if tool_name == "terminal":
        try:
            data = json.loads(result)
            if isinstance(data, dict):
                exit_code = data.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    return True, f" [exit {exit_code}]"
        except (json.JSONDecodeError, TypeError):
            pass
        return False, ""

    # memory 工具看 success 字段
    if tool_name == "memory":
        try:
            data = json.loads(result)
            if isinstance(data, dict):
                if data.get("success") is False and "exceed the limit" in data.get("error", ""):
                    return True, " [full]"
        except (json.JSONDecodeError, TypeError):
            pass

    # 通用检测
    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


# =========================================================================
# 主控制器
# =========================================================================

class ToolCallGuardrailController:
    """每轮工具调用控制器。

    用法：
        controller = ToolCallGuardrailController(config)

        # 每个工具调用前
        decision = controller.before_call(tool_name, args)
        if not decision.allows_execution:
            # 阻止执行

        # 每个工具调用后
        decision = controller.after_call(tool_name, args, result, failed=True/False)
        if decision.action == "warn":
            # 追加警告到结果
    """

    def __init__(self, config: Optional[ToolCallGuardrailConfig] = None):
        self.config = config or ToolCallGuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        """每轮开始时重置所有计数器。"""
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: Optional[ToolGuardrailDecision] = None

    @property
    def halt_decision(self) -> Optional[ToolGuardrailDecision]:
        return self._halt_decision

    def before_call(self, tool_name: str, args: Optional[Mapping[str, Any]]) -> ToolGuardrailDecision:
        """工具调用前的护栏检查。"""
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))

        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        # 检查完全相同参数的失败次数
        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"已阻止 {tool_name}: 相同参数失败了 {exact_count} 次。"
                    "停止不变重试；改变策略或说明阻碍。"
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        # 幂等工具无进展检查
        if self._is_idempotent(tool_name):
            record = self._no_progress.get(signature)
            if record is not None:
                _result_hash, repeat_count = record
                if repeat_count >= self.config.no_progress_block_after:
                    decision = ToolGuardrailDecision(
                        action="block",
                        code="idempotent_no_progress_block",
                        message=(
                            f"已阻止 {tool_name}: 这个只读调用返回了相同结果 "
                            f"{repeat_count} 次。使用已有结果或换不同查询。"
                        ),
                        tool_name=tool_name,
                        count=repeat_count,
                        signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
        result: Optional[str],
        *,
        failed: Optional[bool] = None,
    ) -> ToolGuardrailDecision:
        """工具调用后的护栏检查。"""
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)

        if failed is None:
            failed, _ = classify_tool_failure(tool_name, result)

        if failed:
            # 记录完全相同参数的失败
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            # 记录同一工具的失败
            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # 硬停止：同一工具失败太多次
            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"已停止 {tool_name}: 本轮失败了 {same_count} 次。"
                        "停止重试同一条失败路径，选择不同方法。"
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            # 警告：完全相同参数失败
            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} 已用相同参数失败了 {exact_count} 次。"
                        "这看起来像死循环；检查错误并改变策略。"
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            # 警告：同一工具多次失败
            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=_tool_failure_recovery_hint(tool_name, same_count),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        # 成功：清除失败计数
        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        # 幂等工具无进展检测
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} 返回了相同结果 {repeat_count} 次。"
                    "使用已有结果或改变查询。"
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools


# =========================================================================
# 辅助函数
# =========================================================================

def toolguard_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """为被阻止的工具调用构建合成结果。"""
    return json.dumps({
        "error": decision.message,
        "guardrail": {
            "action": decision.action,
            "code": decision.code,
            "tool_name": decision.tool_name,
            "count": decision.count,
        },
    }, ensure_ascii=False)


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    """将护栏指导追加到工具结果中。"""
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "工具循环硬停止" if decision.action == "halt" else "工具循环警告"
    suffix = f"\n\n[{label}: {decision.code}; 次数={decision.count}; {decision.message}]"
    return (result or "") + suffix


def _tool_failure_recovery_hint(tool_name: str, count: int) -> str:
    """面向恢复的工具失败指导。"""
    common = (
        f"{tool_name} 本轮已失败 {count} 次。这看起来像死循环。"
        "不要切换到纯文本回复；继续使用工具，但先诊断再重试。"
        "首先检查最新错误/输出并验证假设。"
    )
    if tool_name == "terminal":
        return common + (
            "对于终端失败，运行小型诊断如 `pwd && ls -la`，"
            "然后尝试绝对路径、更简单命令、不同工作目录，"
            "或使用 read_file/write_file/patch 等不同工具。"
        )
    return common + (
        "尝试不同参数、更窄的查询/路径、相关时使用绝对路径，"
        "或换用能取得进展的其他工具。"
    )


def _coerce_args(args: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: Optional[str]) -> str:
    try:
        parsed = json.loads(result or "")
        if isinstance(parsed, (dict, list)):
            canonical = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            )
        else:
            canonical = str(parsed)
    except (json.JSONDecodeError, TypeError):
        canonical = result or ""
    return _sha256(canonical)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


__all__ = [
    "ToolCallGuardrailConfig",
    "ToolCallSignature",
    "ToolGuardrailDecision",
    "ToolCallGuardrailController",
    "canonical_tool_args",
    "classify_tool_failure",
    "toolguard_synthetic_result",
    "append_toolguard_guidance",
]
