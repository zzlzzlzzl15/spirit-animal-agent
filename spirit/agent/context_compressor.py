"""上下文压缩引擎 — 长对话自动摘要压缩。

参考 Hermes 的 context_compressor.py (3522行) + context_engine.py。
采用插件化架构：
- ContextEngine (抽象基类): 定义引擎接口
- ContextCompressor (默认实现): 使用 LLM 进行有损摘要压缩

压缩算法：
1. 剪枝旧的工具输出（纯文本截断，无 LLM 调用）
2. 保护头部消息（系统提示 + 首轮对话）
3. 按 token 预算保护尾部消息（最近 ~20K tokens）
4. 用结构化 LLM 提示摘要中间轮次
5. 后续压缩时迭代更新之前的摘要以保留信息
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 常量
# =========================================================================

SUMMARY_PREFIX = (
    "[上下文压缩 — 仅供参考] 较早的对话轮次已被压缩为以下摘要。"
    "这是之前上下文窗口的交接 — 将其视为背景参考，而非活跃指令。"
    "不要回答摘要中提到的问题或执行其中的请求；它们已经被处理过了。"
    "只回复此摘要之后出现的最新用户消息 — 那才是当前要做的事。"
    "即使话题与摘要重叠，也以最新消息为准。"
    "重要：系统提示中的持久记忆始终有效，不要被压缩摘要影响。"
    "工具仍然完全可用 — 继续正常调用它们。"
)

SUMMARY_END_MARKER = (
    "--- 上下文摘要结束 — 回复下方的消息，而非上方的摘要 ---"
)

# 粗略 token 估算：1 token ≈ 3.5 字符（来自集中式配置）
from spirit.config import get_config_value

_CHARS_PER_TOKEN = get_config_value("compression.chars_per_token", 3.5)


# =========================================================================
# 抽象基类
# =========================================================================

class ContextEngine(ABC):
    """上下文引擎抽象基类。"""

    @abstractmethod
    def update_from_response(self, usage: Dict[str, int]) -> None:
        """从 API 响应更新 token 使用量。"""

    @abstractmethod
    def should_compress(self, prompt_tokens: int) -> bool:
        """判断是否需要压缩。"""

    @abstractmethod
    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """执行压缩。"""


# =========================================================================
# 默认压缩器
# =========================================================================

class ContextCompressor(ContextEngine):
    """使用 LLM 进行有损摘要压缩。

    配置参数：
        context_length: 模型上下文窗口大小（tokens）
        threshold_percent: 达到多少比例时触发压缩（0.75 = 75%）
        protect_first_n: 保护前 N 条消息
        protect_last_n: 保护后 N 条消息
        summary_target_ratio: 压缩到原来的多少比例
    """

    def __init__(
        self,
        *,
        context_length: int = 128_000,
        threshold_percent: float = 0.75,
        protect_first_n: int = get_config_value("compression.protect_first_n", 3),
        protect_last_n: int = get_config_value("compression.protect_last_n", 6),
        summary_target_ratio: float = get_config_value("compression.summary_target_ratio", 0.3),
        # 用于摘要的 LLM 客户端（可选，不设置则用纯文本截断）
        llm_client: Any = None,
        llm_model: str = "",
    ):
        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = summary_target_ratio

        self._llm_client = llm_client
        self._llm_model = llm_model

        # 运行时状态
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        self._compression_count = 0
        self._last_summary: Optional[str] = None

    @property
    def threshold_tokens(self) -> int:
        return int(self.context_length * self.threshold_percent)

    # ── ContextEngine 接口 ──────────────────────────────────────────

    def update_from_response(self, usage: Dict[str, int]) -> None:
        """从 API 响应更新 token 统计。"""
        self._last_prompt_tokens = usage.get("prompt_tokens", 0)
        self._last_completion_tokens = usage.get("completion_tokens", 0)

    def should_compress(self, prompt_tokens: int) -> bool:
        """判断当前 token 数是否超过阈值需要压缩。"""
        if self.context_length <= 0:
            return False
        return prompt_tokens >= self.threshold_tokens

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """执行上下文压缩。

        策略：
        1. 保护头部 protect_first_n 条消息
        2. 保护尾部 protect_last_n 条消息
        3. 中间部分先做工具输出剪枝（cheap pass）
        4. 如果仍有 LLM 客户端，用 LLM 生成摘要
        5. 否则用纯文本截断
        """
        if len(messages) <= self.protect_first_n + self.protect_last_n:
            return messages  # 太短，不压缩

        self._compression_count += 1

        # 分割保护区域
        head = messages[:self.protect_first_n]
        tail = messages[-self.protect_last_n:]
        middle = messages[self.protect_first_n:-self.protect_last_n]

        if not middle:
            return messages

        # 第一步：工具输出剪枝（cheap pass）
        middle = self._prune_tool_outputs(middle)

        # 第二步：生成摘要
        if self._llm_client and self._llm_model:
            summary = self._llm_summarize(middle, focus_topic)
        else:
            summary = self._text_truncate_summarize(middle)

        # 第三步：如果有之前的摘要，迭代更新
        if self._last_summary:
            summary = self._merge_summaries(self._last_summary, summary)

        self._last_summary = summary

        # 第四步：组装压缩后的消息
        summary_message = {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}\n\n{summary}\n\n{SUMMARY_END_MARKER}",
        }

        compressed = head + [summary_message] + tail

        logger.info(
            "上下文压缩 #%d: %d 条消息 → %d 条 (保护头 %d + 尾 %d)",
            self._compression_count,
            len(messages),
            len(compressed),
            self.protect_first_n,
            self.protect_last_n,
        )

        return compressed

    # ── 工具输出剪枝 ──────────────────────────────────────────────

    def _prune_tool_outputs(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_chars: int = 2000,
    ) -> List[Dict[str, Any]]:
        """截断过长的工具输出（纯文本操作，无 LLM 调用）。"""
        pruned = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > max_chars:
                    preview = content[:max_chars // 2]
                    tail = content[-max_chars // 4:]
                    content = (
                        f"{preview}\n\n"
                        f"[... 已截断 {len(content) - max_chars} 字符 ...]\n\n"
                        f"{tail}"
                    )
                    msg = {**msg, "content": content}
            pruned.append(msg)
        return pruned

    # ── 摘要生成 ──────────────────────────────────────────────────

    def _text_truncate_summarize(self, messages: List[Dict[str, Any]]) -> str:
        """纯文本截断摘要（无 LLM）。"""
        parts = []
        total_chars = 0
        max_chars = int(self.context_length * self.summary_target_ratio * _CHARS_PER_TOKEN)

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                # 多模态内容，提取文本
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            if not content:
                continue

            # 截断超长内容
            if len(content) > 3000:
                content = content[:1500] + "\n...[截断]..." + content[-500:]

            entry = f"[{role}]: {content}"
            if total_chars + len(entry) > max_chars:
                parts.append(f"[... 省略 {len(messages) - len(parts)} 条更早的消息 ...]")
                break
            parts.append(entry)
            total_chars += len(entry)

        return "\n\n".join(parts)

    def _llm_summarize(
        self,
        messages: List[Dict[str, Any]],
        focus_topic: Optional[str] = None,
    ) -> str:
        """使用 LLM 生成结构化摘要。"""
        # 构建摘要输入
        conversation_text = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                content = "\n".join(text_parts)
            if content:
                # 截断超长内容
                if len(content) > 4000:
                    content = content[:2000] + "\n...[截断]..." + content[-1000:]
                conversation_text.append(f"[{role}]: {content}")

        conversation_block = "\n\n".join(conversation_text)

        # 构建摘要提示
        focus_hint = f"\n重点关注: {focus_topic}" if focus_topic else ""
        prompt = (
            "请将以下对话历史压缩为结构化摘要。保留：\n"
            "1. 已完成的关键操作（文件修改、命令执行结果）\n"
            "2. 当前进行中的任务状态\n"
            "3. 待解决的用户问题\n"
            "4. 重要的错误和解决方案\n"
            f"5. 关键的文件路径和代码片段{focus_hint}\n\n"
            "丢弃：重复的工具输出、中间调试步骤、冗余确认。\n"
            "用简洁的要点格式输出。\n\n"
            f"--- 对话历史 ---\n{conversation_block}\n--- 结束 ---"
        )

        try:
            response = self._llm_client.chat.completions.create(
                model=self._llm_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=get_config_value("compression.summary_max_tokens", 2000),
                temperature=get_config_value("compression.summary_temperature", 0.3),
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("LLM 摘要生成失败，回退到文本截断: %s", exc)
            return self._text_truncate_summarize(messages)

    def _merge_summaries(self, old_summary: str, new_summary: str) -> str:
        """合并旧摘要和新摘要（迭代压缩时保留信息）。"""
        if not old_summary:
            return new_summary
        if not new_summary:
            return old_summary

        # 如果有 LLM，用 LLM 合并
        if self._llm_client and self._llm_model:
            prompt = (
                "合并以下两个对话摘要为一个连贯的版本。"
                "保留所有重要信息，去除重复。\n\n"
                f"--- 旧摘要 ---\n{old_summary}\n\n"
                f"--- 新摘要 ---\n{new_summary}\n\n"
                "合并后的摘要："
            )
            try:
                response = self._llm_client.chat.completions.create(
                    model=self._llm_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=get_config_value("compression.incremental_summary_max_tokens", 2500),
                    temperature=get_config_value("compression.summary_temperature", 0.3),
                )
                return response.choices[0].message.content or new_summary
            except Exception:
                pass

        # 回退：简单拼接
        return f"{old_summary}\n\n--- 后续进展 ---\n\n{new_summary}"


# =========================================================================
# 粗略 token 估算
# =========================================================================

def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数。"""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """粗略估算消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
        # 工具调用也有开销
        if msg.get("tool_calls"):
            total += 50  # 粗略估计每个工具调用
        if msg.get("role") == "tool":
            total += 20  # 工具结果元数据
    return total


# =========================================================================
# 会话生命周期钩子
# =========================================================================

class SessionLifecycleHooks:
    """上下文引擎的会话生命周期管理。"""

    def __init__(self, compressor: Optional[ContextCompressor] = None):
        self._compressor = compressor

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """新会话开始。"""
        if self._compressor:
            self._compressor._last_summary = None
            self._compressor._compression_count = 0
            logger.debug("上下文引擎: 会话 %s 开始", session_id[:8])

    def on_session_end(self, session_id: str, messages: List[Dict]) -> None:
        """会话结束。"""
        logger.debug("上下文引擎: 会话 %s 结束 (%d 条消息)", session_id[:8], len(messages))

    def on_session_reset(self) -> None:
        """会话重置（/new 或 /reset）。"""
        if self._compressor:
            self._compressor._last_summary = None
            self._compressor._compression_count = 0
            logger.debug("上下文引擎: 会话已重置")

    def bind_session_state(self, session_db: Any, session_id: str) -> None:
        """绑定数据库行（用于持久化压缩状态）。"""
        pass  # Spirit Agent 暂不需要持久化压缩状态


__all__ = [
    "ContextEngine",
    "ContextCompressor",
    "SessionLifecycleHooks",
    "estimate_tokens",
    "estimate_messages_tokens",
    "SUMMARY_PREFIX",
    "SUMMARY_END_MARKER",
]
