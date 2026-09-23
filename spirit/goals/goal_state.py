"""Goal 状态数据模型 — Ralph Loop 的持久化状态。

参考 Hermes ``hermes_cli/goals.py`` 的 GoalContract / GoalState 设计：

- :class:`GoalContract` — 可选的结构化"完成契约"（outcome / verification /
  constraints / boundaries / stop_when）。它把"什么叫做完、如何证明、不能破坏
  什么、范围在哪、何时该停下问人"显式写清楚，让 judge 依据证据而非感觉裁决。
- :class:`GoalState` — 每会话持久化的目标状态（含 subgoals 子目标 + wait
  barrier 等待屏障）。可 JSON 序列化，存入 SessionDB 的 ``state_meta`` 表
  （key = ``goal:<session_id>``），因此 ``/resume`` 能重新捡起未完成的目标。
- :func:`parse_contract` — 解析用户一次性输入的 inline ``field: value`` 契约。

设计原则（对齐 Hermes，不可协商）：

- 无契约的自由目标完全兼容——每个契约字段默认空，渲染/入 prompt 时整段省略，
  行为与"最初的纯自由文本目标"逐字节一致。
- 向后兼容：旧的 ``state_meta`` 行加载时，缺失字段一律用安全默认值填充。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────
# 常量与默认值
# ──────────────────────────────────────────────────────────────────────

# 一个目标默认最多自主推进多少轮（每轮 = 一次 judge 裁决 + 可能的续传）。
# 预算耗尽后目标自动 paused，等用户 /goal resume 或 /goal clear。
DEFAULT_MAX_TURNS = 20

# 连续这么多次 judge 输出无法解析（空/非 JSON）后自动暂停，避免弱 judge
# 模型把整个 turn 预算烧光。API/网络错误不计入（那是瞬时的，fail-open）。
DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES = 3

# 完成契约的五个字段（展示顺序）。改编自 OpenAI Codex 的 "strong goal" 指导：
# 一个耐用的目标最好点名"做完是什么样、如何证明、什么不能退化、哪些路径/工具
# 在范围内、何时该停下问人"。纯自由目标（无契约）始终完全支持。
_CONTRACT_FIELDS = ("outcome", "verification", "constraints", "boundaries", "stop_when")

# 渲染 + inline 解析用的人类可读标签。
_CONTRACT_LABELS = {
    "outcome": "Outcome",
    "verification": "Verification",
    "constraints": "Constraints",
    "boundaries": "Boundaries",
    "stop_when": "Stop when blocked",
}

# 用户可能在值前输入的 inline 别名，映射到规范字段名。
# 例如 ``verify: tests pass`` 或 ``done when: ...``。
_CONTRACT_ALIASES = {
    "outcome": "outcome",
    "goal": "outcome",
    "done": "outcome",
    "done when": "outcome",
    "verification": "verification",
    "verify": "verification",
    "verified by": "verification",
    "evidence": "verification",
    "proof": "verification",
    "constraints": "constraints",
    "constraint": "constraints",
    "preserve": "constraints",
    "must not": "constraints",
    "do not change": "constraints",
    "boundaries": "boundaries",
    "boundary": "boundaries",
    "scope": "boundaries",
    "allowed": "boundaries",
    "files": "boundaries",
    "stop when": "stop_when",
    "stop_when": "stop_when",
    "blocked": "stop_when",
    "stop if blocked": "stop_when",
    "give up when": "stop_when",
}


# ──────────────────────────────────────────────────────────────────────
# 完成契约
# ──────────────────────────────────────────────────────────────────────

@dataclass
class GoalContract:
    """一个目标的可选结构化完成契约。

    每个字段都是用户（或 :func:`spirit.goals.judge.draft_contract`）提供的
    自由文本。空字段在所有地方被省略——无契约的目标行为与最初的纯自由目标
    完全一致。契约会被织入续传 prompt（让 Agent 瞄准验证面、遵守约束）和
    judge prompt（让"done"依据证据裁决，而非凭感觉）。
    """

    outcome: str = ""        # 做完时必须为真的单一终态
    verification: str = ""   # 证明 outcome 的具体测试/命令/产物（必须可核查）
    constraints: str = ""    # 不能改变或退化的东西
    boundaries: str = ""     # 哪些文件/目录/工具/系统在范围内
    stop_when: str = ""      # 何种条件下应停下问人，而不是硬推

    def is_empty(self) -> bool:
        """所有字段都为空 → 空契约（调用方会整段跳过）。"""
        return not any(getattr(self, f).strip() for f in _CONTRACT_FIELDS)

    def to_dict(self) -> Dict[str, str]:
        return {f: getattr(self, f) for f in _CONTRACT_FIELDS}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "GoalContract":
        if not isinstance(data, dict):
            return cls()
        return cls(**{f: str(data.get(f) or "").strip() for f in _CONTRACT_FIELDS})

    def render_block(self) -> str:
        """把非空契约字段渲染为带标签的块。空契约 → 空字符串。"""
        lines = []
        for f in _CONTRACT_FIELDS:
            val = getattr(self, f).strip()
            if val:
                lines.append(f"- {_CONTRACT_LABELS[f]}: {val}")
        return "\n".join(lines)


def parse_contract(text: str) -> Tuple[str, GoalContract]:
    """把用户输入的目标文本拆成 headline + 结构化契约。

    支持 inline ``field: value`` 行，让高级用户一次性输入完整契约，例如::

        Migrate auth to JWT
        verify: the auth test suite passes
        constraints: keep the public /login response shape unchanged
        boundaries: only touch services/auth and its tests
        stop when: a schema change needs product sign-off

    第一条（或多条）非字段行成为目标 headline；被识别的 ``field:`` 行填充
    契约。同一字段的多行会被合并。未识别的前缀保留在 headline 里，所以一个
    恰好带冒号的普通自由目标（``Fix bug: the parser``）不会被破坏——只有前缀
    匹配已知别名的行才会被抽出来。返回 ``(headline, contract)``。
    """
    if not text:
        return "", GoalContract()

    headline_parts: List[str] = []
    fields: Dict[str, List[str]] = {f: [] for f in _CONTRACT_FIELDS}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = False
        if ":" in line:
            prefix, _, value = line.partition(":")
            key = _CONTRACT_ALIASES.get(prefix.strip().lower())
            if key is not None and value.strip():
                fields[key].append(value.strip())
                matched = True
        if not matched:
            headline_parts.append(line)

    headline = " ".join(headline_parts).strip()
    contract = GoalContract(**{f: " ".join(v).strip() for f, v in fields.items()})
    # 若给了 headline 但没有显式 `outcome:` 字段，headline 本身就是 outcome——
    # 不要再把它复制进契约块（目标文本已经带着它了），所以这种情况 outcome 留空。
    return headline, contract


# ──────────────────────────────────────────────────────────────────────
# 目标状态
# ──────────────────────────────────────────────────────────────────────

@dataclass
class GoalState:
    """每会话存储的可序列化目标状态。"""

    goal: str
    status: str = "active"          # active | paused | done | cleared
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    last_verdict: Optional[str] = None        # "done" | "continue" | "wait" | "skipped"
    last_reason: Optional[str] = None
    paused_reason: Optional[str] = None       # 为何自动暂停（预算耗尽等）
    consecutive_parse_failures: int = 0       # 连续 judge 输出解析失败次数
    # 用户中途通过 /subgoal 命令追加的额外验收标准。非空时 judge prompt 和
    # 续传 prompt 都会带上它们，让 Agent 朝它们努力、judge 把它们纳入裁决。
    # 向后兼容：默认空，旧 state_meta 行原样加载。
    subgoals: List[str] = field(default_factory=list)
    # 等待屏障：当 Agent 卡在长时运行的异步工作上（CI 轮询、构建、测试、部署、
    # 限流冷却）时，目标循环 PARK（泊车）而不是每轮被反复戳去做无用功。三种屏障，
    # 由 judge 自动设置（它能看到后台进程列表并返回 wait 裁决）或通过 /goal wait
    # 手动设置：
    #   • waiting_on_pid     — 泊车直到该进程退出。
    #   • waiting_on_session — 泊车直到该 process_registry 会话自己的触发器命中。
    #   • waiting_until      — 泊车直到这个 wall-clock epoch（时间退避）。
    # 任一激活时，evaluate_after_turn 短路为 should_continue=False，不烧 turn、
    # 不调 judge。屏障在 pid 退出 / 触发器命中 / 截止时刻到达后自动清除。
    waiting_on_pid: Optional[int] = None
    waiting_on_session: Optional[str] = None
    waiting_until: float = 0.0
    waiting_reason: Optional[str] = None
    waiting_since: float = 0.0
    # 可选的结构化完成契约。默认空；无契约的目标行为与最初的纯自由目标一致。
    contract: GoalContract = field(default_factory=GoalContract)

    def to_json(self) -> str:
        data = asdict(self)
        # asdict 已把 GoalContract 递归成普通 dict。
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "GoalState":
        data = json.loads(raw)
        raw_subgoals = data.get("subgoals") or []
        subgoals: List[str] = []
        if isinstance(raw_subgoals, list):
            subgoals = [str(s).strip() for s in raw_subgoals if str(s).strip()]
        return cls(
            goal=data.get("goal", ""),
            status=data.get("status", "active"),
            turns_used=int(data.get("turns_used", 0) or 0),
            max_turns=int(data.get("max_turns", DEFAULT_MAX_TURNS) or DEFAULT_MAX_TURNS),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            last_turn_at=float(data.get("last_turn_at", 0.0) or 0.0),
            last_verdict=data.get("last_verdict"),
            last_reason=data.get("last_reason"),
            paused_reason=data.get("paused_reason"),
            consecutive_parse_failures=int(data.get("consecutive_parse_failures", 0) or 0),
            subgoals=subgoals,
            waiting_on_pid=(int(data["waiting_on_pid"]) if data.get("waiting_on_pid") else None),
            waiting_on_session=(str(data["waiting_on_session"]) if data.get("waiting_on_session") else None),
            waiting_until=float(data.get("waiting_until", 0.0) or 0.0),
            waiting_reason=data.get("waiting_reason"),
            waiting_since=float(data.get("waiting_since", 0.0) or 0.0),
            contract=GoalContract.from_dict(data.get("contract")),
        )

    # --- 契约辅助 ------------------------------------------------------

    def has_contract(self) -> bool:
        return self.contract is not None and not self.contract.is_empty()

    # --- 子目标辅助 ----------------------------------------------------

    def render_subgoals_block(self) -> str:
        """把子目标渲染为编号的 ``- N. text`` 块。无子目标时返回空串。"""
        if not self.subgoals:
            return ""
        return "\n".join(f"- {i}. {text}" for i, text in enumerate(self.subgoals, start=1))

    def fresh(self) -> "GoalState":
        """返回一个刚 set() 时的等价状态（用于测试对照）。"""
        return GoalState(goal=self.goal, max_turns=self.max_turns, created_at=time.time())


__all__ = [
    "GoalContract",
    "GoalState",
    "parse_contract",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES",
]
