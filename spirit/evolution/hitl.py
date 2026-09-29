"""可选人在环（HITL）—— Spirit Agent Phase 6.E（docs/13 §七 / §Phase E）。

行为契约（docs/13 §七）：

=========================  ==================================================
场景                        行为
=========================  ==================================================
正常探索、置信度充足        **不打扰**，自主进化
路线分叉 / 重大决策 / 低置信  **主动询问**（推送活跃渠道），附选项与利弊
用户给出反馈                注入 Curriculum 上下文 + 写入记忆（source=user）
询问后超时无响应            **fail-open**：按当前最优自主决策并继续，不卡死
``mode=never``              全程自主，永不询问
``mode=always``             每个关键节点都询问（调试 / 高管控）
=========================  ==================================================

**注入式可测试**：询问通道经 ``ask`` seam 注入（``(query, timeout) -> Optional[str]``，
返回 ``None`` 表示超时/无响应）。真实部署时可桥接 :mod:`spirit.gateway` 澄清队列；核心逻辑
保持同步、离线、确定性。fail-open 是铁律：任何异常/超时都不得卡死闭环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0            # 询问超时（秒）
DEFAULT_CONFIDENCE_THRESHOLD = 0.5  # 低于此置信度触发询问


class HITLMode(str, Enum):
    """人在环模式（对标 AutoGen ``human_input_mode`` 语义，docs/13 §E5）。"""

    AUTO = "auto"      # 仅低置信度 / 分叉点询问
    ALWAYS = "always"  # 每个关键节点都询问
    NEVER = "never"    # 全程自主，永不询问


# 询问 seam：接收 HumanQuery + 超时，返回用户答复文本；None=超时/无响应
AskFn = Callable[["HumanQuery", float], Optional[str]]


def parse_mode(raw: Any) -> HITLMode:
    """把任意输入容错解析为 :class:`HITLMode`（未知 → AUTO）。"""
    if isinstance(raw, HITLMode):
        return raw
    text = str(raw or "").strip().lower()
    for member in HITLMode:
        if member.value == text:
            return member
    return HITLMode.AUTO


def resolve_mode(agent: Any = None) -> HITLMode:
    """解析 HITL 模式：``evolution.hitl.mode`` 配置 > ``agent`` 属性 > AUTO。"""
    try:
        from spirit.config import get_config_value
        configured = get_config_value("evolution.hitl.mode", None)
        if configured:
            return parse_mode(configured)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("evolution.hitl: 读取 evolution.hitl.mode 失败: %s", exc)
    mode = getattr(agent, "hitl_mode", None)
    return parse_mode(mode) if mode else HITLMode.AUTO


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class HumanQuery:
    """一次向用户发起的询问（附选项与利弊，docs/13 §七）。"""

    question: str = ""
    options: List[str] = field(default_factory=list)
    context: str = ""
    confidence: float = 1.0     # 触发方（Curriculum/Verifier）的置信度
    is_fork: bool = False       # 是否路线分叉 / 重大决策
    task_id: str = ""

    def render(self) -> str:
        parts = [self.question or "（无问题描述）"]
        if self.options:
            opts = "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(self.options))
            parts.append(f"可选项：\n{opts}")
        if self.context:
            parts.append(f"背景：\n{self.context}")
        return "\n\n".join(parts)


@dataclass
class HumanAnswer:
    """询问的结果（区分"用户已答" / "超时自主" / "未触发自主"）。"""

    text: str = ""
    answered: bool = False          # 是否真的收到用户答复
    asked: bool = False             # 是否实际发起了询问
    timed_out: bool = False         # 发起后超时无响应（fail-open）
    source: str = "auto"            # user | auto

    @property
    def from_user(self) -> bool:
        return self.source == "user" and self.answered

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "answered": self.answered,
            "asked": self.asked,
            "timed_out": self.timed_out,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# HITL 编排
# ---------------------------------------------------------------------------

class HumanInTheLoop:
    """人在环编排器：触发判定 → 询问 → 超时自主（fail-open）→ 建议回灌记忆。"""

    def __init__(
        self,
        *,
        mode: HITLMode | str = HITLMode.AUTO,
        ask: Optional[AskFn] = None,
        memory: Any = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        timeout: float = DEFAULT_TIMEOUT,
        auto_decide: Optional[Callable[[HumanQuery], str]] = None,
    ) -> None:
        self.mode = parse_mode(mode)
        self.ask = ask
        self.memory = memory
        self.confidence_threshold = confidence_threshold
        self.timeout = timeout
        self._auto_decide = auto_decide

    # -- 触发判定（E1）-----------------------------------------------------
    def should_ask(self, query: HumanQuery) -> bool:
        """是否该就此 query 打扰用户。"""
        if self.mode is HITLMode.NEVER:
            return False
        if self.mode is HITLMode.ALWAYS:
            return True
        # AUTO：低置信度 或 路线分叉/重大决策
        return bool(query.is_fork) or query.confidence < self.confidence_threshold

    # -- 默认自主决策（fail-open 用）--------------------------------------
    def default_decide(self, query: HumanQuery) -> str:
        """无用户输入时的最优自主决策：优先注入的 ``auto_decide``，否则取首个选项。"""
        if self._auto_decide is not None:
            try:
                return str(self._auto_decide(query) or "")
            except Exception as exc:  # pragma: no cover - 自定义决策异常降级
                logger.warning("evolution.hitl: auto_decide 异常: %s", exc)
        return query.options[0] if query.options else ""

    # -- 主入口 ------------------------------------------------------------
    def consult(self, query: HumanQuery) -> HumanAnswer:
        """就 ``query`` 咨询用户；按模式/触发决定问不问，超时一律 fail-open 自主继续。"""
        if not self.should_ask(query):
            return HumanAnswer(text=self.default_decide(query), source="auto")

        # 需要问，但没有可用询问通道 → fail-open 自主决策（不卡死）
        if self.ask is None:
            logger.debug("evolution.hitl: 需询问但无 gateway，fail-open 自主决策")
            return HumanAnswer(text=self.default_decide(query), asked=False, source="auto")

        try:
            reply = self.ask(query, self.timeout)
        except Exception as exc:
            logger.warning("evolution.hitl: 询问通道异常 → fail-open: %s", exc)
            return HumanAnswer(
                text=self.default_decide(query), asked=True, timed_out=True, source="auto"
            )

        if reply is None or not str(reply).strip():
            # 超时 / 无响应 → fail-open（docs/13 §七）
            return HumanAnswer(
                text=self.default_decide(query), asked=True, timed_out=True, source="auto"
            )

        answer = HumanAnswer(text=str(reply).strip(), answered=True, asked=True, source="user")
        self.record_suggestion(query, answer)  # E4：建议持久化
        return answer

    # -- 建议回灌（E4）-----------------------------------------------------
    def record_suggestion(self, query: HumanQuery, answer: HumanAnswer) -> bool:
        """把用户路线选择写入记忆（source=user），后续决策优先参考。"""
        if self.memory is None or not answer.from_user:
            return False
        try:
            self.memory.add_insight(
                text=f"[用户建议] {query.question} → {answer.text}",
                confidence=0.95,          # 用户拍板，高置信
                source_task_id=query.task_id or "hitl",
                tags=["user", "hitl"],
            )
            return True
        except Exception as exc:  # pragma: no cover - 记忆写入失败不阻断
            logger.warning("evolution.hitl: 建议写入记忆失败: %s", exc)
            return False


def make_hitl(
    *,
    mode: HITLMode | str = HITLMode.AUTO,
    ask: Optional[AskFn] = None,
    memory: Any = None,
    **kwargs: Any,
) -> HumanInTheLoop:
    """便捷构造 :class:`HumanInTheLoop`。"""
    return HumanInTheLoop(mode=mode, ask=ask, memory=memory, **kwargs)


__all__ = [
    "HITLMode",
    "HumanQuery",
    "HumanAnswer",
    "HumanInTheLoop",
    "AskFn",
    "parse_mode",
    "resolve_mode",
    "make_hitl",
    "DEFAULT_TIMEOUT",
    "DEFAULT_CONFIDENCE_THRESHOLD",
]
