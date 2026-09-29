"""自进化三角色 —— prompt 模板 + model slot 解析 + 结构化输出（Phase 6.A）。

对标 RSIAgent 的三角色协议（docs/13 §2.1），按 Spirit「精简子集 + 可测试」约定：

- **Actor**：操作环境、读自己的持久记忆、蒸馏已验证经验（执行 + 学习合一）。
- **Verifier**：独立检查候选证据，**看不到 Actor 的私有推理与记忆**（防作弊），
  给出 PASS / FAIL / UNVERIFIED + 理由（理由必须非空）。
- **Curriculum**：复盘学习进度，选下一个练习/目标任务 + 继续/停止决策
  （**不打分、不写 Actor 记忆**）。

模型调用**注入式可测试**：复用 :data:`spirit.goals.judge.LLMCaller` 签名
``(messages, temperature, max_tokens, timeout) -> str``。无 caller（离线/测试）时
各角色 fail-soft 产出**安全默认**（Verifier→UNVERIFIED、Curriculum→STALLED），
绝不把"没调用"误判成 PASS/FAIL 或语义化决策（docs/13 §2.3 铁律）。

model slot 解析优先级：``evolution.roles.<role>.model`` 配置 > ``agent.model``。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from spirit.evolution.protocol import (
    ActorLearning,
    CurriculumDecision,
    CurriculumDecisionKind,
    RoleName,
    TargetVerdict,
    Verdict,
)

logger = logging.getLogger(__name__)

# 复用 goals.judge 的 llm_caller 签名（(messages, temperature, max_tokens, timeout) -> str）
LLMCaller = Callable[[List[Dict[str, str]], float, int, float], str]

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 2048

# 贪婪匹配最外层 JSON 对象；失败再退化为非贪婪
_JSON_GREEDY_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_LAZY_RE = re.compile(r"\{.*?\}", re.DOTALL)


# ---------------------------------------------------------------------------
# 角色规格 + prompt 模板
# ---------------------------------------------------------------------------

_ACTOR_SYSTEM = """\
你是自进化闭环中的 **Actor（执行者 + 学习者）**。
职责：在给定领域环境中执行任务，观察结果，并把这次经验蒸馏成可复用知识。
约束：
- 你只能依据自己在环境中的真实动作与观察，不得凭空编造成功。
- 蒸馏必须包含**诊断**（这次为什么成功/失败、下次该怎么做），诊断不得为空。
- 输出严格为一个 JSON 对象，不要额外文字：
  {"memory": "<可复用的经验要点>", "diagnosis": "<对本次结果的诊断与改进方向>"}
"""

_VERIFIER_SYSTEM = """\
你是自进化闭环中的 **Verifier（独立校验者）**。
职责：仅依据客观证据（环境状态、日志、产物、回测/测试结果）独立判断任务是否达成。
约束（防作弊）：
- 你**看不到** Actor 的私有推理与记忆，只能看"任务要求 + 候选证据"。
- 客观信号优先于模型自评；证据不足或基础设施异常 → 判 UNVERIFIED（绝不当 PASS/FAIL）。
- 裁决必须附**非空理由**，说明依据了哪些证据。
- 输出严格为一个 JSON 对象，不要额外文字：
  {"verdict": "PASS|FAIL|UNVERIFIED", "reason": "<依据的证据与判断理由>", "confidence": <0.0-1.0>}
"""

_CURRICULUM_SYSTEM = """\
你是自进化闭环中的 **Curriculum（课程规划者）**。
职责：复盘学习进度，决定下一步——派一个练习项目、判定就绪可重试目标、收敛结束、或标记卡死。
约束：
- 你**不打分、不写 Actor 的记忆**；记忆视图默认只读。
- "卡死(stalled)"必须与"成功收敛(converged)"严格区分：预算耗尽/无进展 → stalled。
- 输出严格为一个 JSON 对象，不要额外文字：
  {"decision": "practice|ready|converged|stalled", "next_task": "<下一步任务描述>", "rationale": "<决策理由>"}
"""


@dataclass(frozen=True)
class RoleSpec:
    """单个角色的规格：prompt 模板 + model slot 配置键 + 采样参数。"""

    name: RoleName
    display_name: str
    system_prompt: str
    model_config_key: str
    temperature: float
    max_tokens: int

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name.value,
            "display_name": self.display_name,
            "model_config_key": self.model_config_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


ROLE_SPECS: Dict[RoleName, RoleSpec] = {
    RoleName.ACTOR: RoleSpec(
        name=RoleName.ACTOR,
        display_name="Actor（执行者）",
        system_prompt=_ACTOR_SYSTEM,
        model_config_key="evolution.roles.actor.model",
        temperature=0.3,
        max_tokens=DEFAULT_MAX_TOKENS,
    ),
    RoleName.VERIFIER: RoleSpec(
        name=RoleName.VERIFIER,
        display_name="Verifier（校验者）",
        system_prompt=_VERIFIER_SYSTEM,
        model_config_key="evolution.roles.verifier.model",
        temperature=0.0,  # 裁决要确定性
        max_tokens=DEFAULT_MAX_TOKENS,
    ),
    RoleName.CURRICULUM: RoleSpec(
        name=RoleName.CURRICULUM,
        display_name="Curriculum（课程规划者）",
        system_prompt=_CURRICULUM_SYSTEM,
        model_config_key="evolution.roles.curriculum.model",
        temperature=0.0,
        max_tokens=DEFAULT_MAX_TOKENS,
    ),
}


def get_role_spec(role: RoleName | str) -> RoleSpec:
    """按名取角色规格（容错字符串）。未知角色回退到 Actor。"""
    if isinstance(role, str):
        try:
            role = RoleName(role.lower())
        except ValueError:
            logger.warning("evolution.roles: 未知角色 %r，回退到 actor", role)
            role = RoleName.ACTOR
    return ROLE_SPECS[role]


# ---------------------------------------------------------------------------
# model slot 解析 + caller 构造
# ---------------------------------------------------------------------------

def resolve_model(role: RoleName | str, agent: Any = None) -> Optional[str]:
    """解析某角色应使用的模型。

    优先级：``evolution.roles.<role>.model`` 配置 > ``agent.model`` > None。
    三角色可各配不同档位模型（docs/13 §9：Actor 用强模型、Verifier/Curriculum 用便宜模型）。
    """
    spec = get_role_spec(role)
    try:
        from spirit.config import get_config_value
        configured = get_config_value(spec.model_config_key, None)
        if configured:
            return str(configured)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("evolution.roles: 读取 %s 失败: %s", spec.model_config_key, exc)
    model = getattr(agent, "model", None)
    return str(model) if model else None


def build_role_caller(agent: Any, role: RoleName | str) -> LLMCaller:
    """从一个 SpiritAgent 构造某角色专用的 ``llm_caller`` 闭包。

    复用 Agent 的 OpenAI 兼容 ``client`` 发起 side call（不碰对话历史/系统提示词，
    prompt cache 不受影响）；模型取 :func:`resolve_model`（角色 slot > 主对话模型）。
    对齐 :func:`spirit.goals.judge.build_agent_llm_caller` 范式。
    """
    spec = get_role_spec(role)

    def caller(
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> str:
        model = resolve_model(spec.name, agent) or getattr(agent, "model", None)
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens and max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        try:
            resp = agent.client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("evolution.roles: %s 调用失败: %s", spec.name.value, exc)
            return ""

    return caller


def render_messages(role: RoleName | str, user_content: str) -> List[Dict[str, str]]:
    """渲染某角色的一次调用消息（system + user）。"""
    spec = get_role_spec(role)
    return [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# 结构化输出解析（fail-soft）
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """从原始回复里提取首个 JSON 对象。贪婪优先，失败退化非贪婪；都不行返回 None。"""
    if not raw:
        return None
    for regex in (_JSON_GREEDY_RE, _JSON_LAZY_RE):
        match = regex.search(raw)
        if not match:
            continue
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_actor_learning(raw: str, *, task_id: str = "") -> ActorLearning:
    """解析 Actor 回复为 :class:`ActorLearning`。

    有 JSON 用 JSON；无 JSON 则整段作为 memory、diagnosis 留空（``is_valid()`` 为 False，
    由外层判定是否算有效学习）。
    """
    obj = _extract_json(raw)
    if obj is None:
        return ActorLearning(memory=(raw or "").strip(), diagnosis="", task_id=task_id)
    return ActorLearning(
        memory=str(obj.get("memory", "") or ""),
        diagnosis=str(obj.get("diagnosis", "") or ""),
        task_id=task_id,
    )


def parse_verdict(raw: str, *, task_id: str = "") -> Verdict:
    """解析 Verifier 回复为 :class:`Verdict`。

    无法解析 → UNVERIFIED（铁律：解析失败绝不当 PASS/FAIL），理由带原始片段以便审计。
    """
    obj = _extract_json(raw)
    if obj is None:
        reason = (raw or "").strip()
        if not reason:
            reason = "无法解析 Verifier 输出（空回复）"
        return Verdict(
            verdict=TargetVerdict.UNVERIFIED, reason=reason, task_id=task_id
        )
    reason = str(obj.get("reason", "") or "").strip()
    if not reason:
        reason = "Verifier 未提供理由"
    try:
        confidence = float(obj.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return Verdict(
        verdict=obj.get("verdict", TargetVerdict.UNVERIFIED),
        reason=reason,
        task_id=task_id,
        confidence=confidence,
    )


def parse_curriculum_decision(raw: str) -> CurriculumDecision:
    """解析 Curriculum 回复为 :class:`CurriculumDecision`。

    无法解析 → STALLED（安全侧：不因解析失败而误判为成功收敛）。
    """
    obj = _extract_json(raw)
    if obj is None:
        rationale = (raw or "").strip() or "无法解析 Curriculum 输出"
        return CurriculumDecision(
            decision=CurriculumDecisionKind.STALLED, next_task="", rationale=rationale
        )
    return CurriculumDecision(
        decision=obj.get("decision", CurriculumDecisionKind.STALLED),
        next_task=str(obj.get("next_task", "") or ""),
        rationale=str(obj.get("rationale", "") or ""),
    )


# ---------------------------------------------------------------------------
# 角色运行器（注入式 caller）
# ---------------------------------------------------------------------------

class Role:
    """角色基类：持有规格 + 可选注入式 caller，负责渲染 prompt 并调用 LLM。"""

    def __init__(self, spec: RoleSpec, caller: Optional[LLMCaller] = None) -> None:
        self.spec = spec
        self._caller = caller

    @property
    def name(self) -> RoleName:
        return self.spec.name

    def has_caller(self) -> bool:
        return self._caller is not None

    def invoke(
        self,
        user_content: str,
        *,
        caller: Optional[LLMCaller] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        """发一次角色调用，返回原始文本。无 caller → 返回空串（由子类产出安全默认）。"""
        active = caller or self._caller
        if active is None:
            logger.debug("evolution.roles: %s 无 llm_caller，跳过调用", self.spec.name.value)
            return ""
        messages = render_messages(self.spec.name, user_content)
        try:
            return active(
                messages, self.spec.temperature, self.spec.max_tokens, timeout
            ) or ""
        except Exception as exc:
            logger.warning("evolution.roles: %s 调用异常: %s", self.spec.name.value, exc)
            return ""


class ActorRole(Role):
    """Actor：执行 + 蒸馏经验。"""

    def __init__(self, caller: Optional[LLMCaller] = None) -> None:
        super().__init__(ROLE_SPECS[RoleName.ACTOR], caller)

    def learn(
        self,
        task: str,
        observation: str = "",
        *,
        task_id: str = "",
        caller: Optional[LLMCaller] = None,
    ) -> ActorLearning:
        user = f"任务：\n{task}\n\n你的执行观察/结果：\n{observation or '（无）'}"
        raw = self.invoke(user, caller=caller)
        if not raw:
            return ActorLearning(memory="", diagnosis="", task_id=task_id)
        return parse_actor_learning(raw, task_id=task_id)


class VerifierRole(Role):
    """Verifier：独立证据校验（屏蔽 Actor 私有推理/记忆）。"""

    def __init__(self, caller: Optional[LLMCaller] = None) -> None:
        super().__init__(ROLE_SPECS[RoleName.VERIFIER], caller)

    def verify(
        self,
        task: str,
        evidence: str,
        *,
        task_id: str = "",
        caller: Optional[LLMCaller] = None,
    ) -> Verdict:
        user = f"任务要求：\n{task}\n\n候选证据（环境状态/日志/产物）：\n{evidence or '（无证据）'}"
        raw = self.invoke(user, caller=caller)
        if not raw:
            # 无 caller / 调用失败 = 基础设施不可用 → UNVERIFIED（绝不当 PASS/FAIL）
            return Verdict(
                verdict=TargetVerdict.UNVERIFIED,
                reason="Verifier 未产出裁决（无 llm_caller 或调用失败）",
                task_id=task_id,
            )
        return parse_verdict(raw, task_id=task_id)


class CurriculumRole(Role):
    """Curriculum：派任务 + 继续/停止决策（不打分、不写 Actor 记忆）。"""

    def __init__(self, caller: Optional[LLMCaller] = None) -> None:
        super().__init__(ROLE_SPECS[RoleName.CURRICULUM], caller)

    def decide(
        self,
        task: str,
        progress: str = "",
        *,
        caller: Optional[LLMCaller] = None,
    ) -> CurriculumDecision:
        user = f"当前目标任务：\n{task}\n\n学习进度/历史：\n{progress or '（无）'}"
        raw = self.invoke(user, caller=caller)
        if not raw:
            return CurriculumDecision(
                decision=CurriculumDecisionKind.STALLED,
                next_task="",
                rationale="Curriculum 未产出决策（无 llm_caller 或调用失败）",
            )
        return parse_curriculum_decision(raw)


def make_actor(caller: Optional[LLMCaller] = None) -> ActorRole:
    return ActorRole(caller)


def make_verifier(caller: Optional[LLMCaller] = None) -> VerifierRole:
    return VerifierRole(caller)


def make_curriculum(caller: Optional[LLMCaller] = None) -> CurriculumRole:
    return CurriculumRole(caller)


__all__ = [
    "LLMCaller",
    "RoleSpec",
    "ROLE_SPECS",
    "Role",
    "ActorRole",
    "VerifierRole",
    "CurriculumRole",
    "get_role_spec",
    "resolve_model",
    "build_role_caller",
    "render_messages",
    "parse_actor_learning",
    "parse_verdict",
    "parse_curriculum_decision",
    "make_actor",
    "make_verifier",
    "make_curriculum",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_TOKENS",
]
