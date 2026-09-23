"""Goal judge — 用辅助模型裁决"目标达成了吗"。

参考 Hermes ``hermes_cli/goals.py`` 的 judge_goal / _parse_judge_response /
draft_contract，但做了一处关键架构适配：

**Hermes** 内部 ``from agent.auxiliary_client import call_llm`` 直接调辅助模型；
**Spirit 没有 auxiliary_client**，所以 :func:`judge_goal` 改为接收一个可注入的
``llm_caller`` 回调（签名 ``(messages, temperature, max_tokens, timeout) -> str``）。
这样：

- judge_goal / _parse_judge_response 都是**纯函数**，测试可传一个返回固定字符串
  的假 caller，无需真实 LLM——对齐 Hermes 测试用 ``patch("...judge_goal")`` 的思路。
- 生产环境由 :func:`build_agent_llm_caller` 从 ``SpiritAgent.client`` 构造 caller，
  复用主对话的 OpenAI 兼容客户端发起一次 side call。

裁决三态（对齐 Hermes）：

- ``done``     — 目标已满足（或明确阻塞/需人介入，也记 done 并说明原因）。
- ``continue`` — 未完成，且现在就有可执行的下一步（存疑时的默认）。
- ``wait``     — 未完成，但下一步是等异步活干完（后台进程/限流冷却），泊车不烧 turn。

失败一律 fail-OPEN 为 ``continue``：坏掉的 judge 绝不能卡死进度，turn 预算兜底。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from spirit.goals.goal_state import GoalContract
from spirit.goals.prompts import (
    DRAFT_CONTRACT_SYSTEM_PROMPT,
    JUDGE_BACKGROUND_BLOCK_TEMPLATE,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_USER_PROMPT_TEMPLATE,
    JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE,
    JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 常量与类型
# ──────────────────────────────────────────────────────────────────────

DEFAULT_JUDGE_TIMEOUT = 30.0
# judge 输出预算。自由格式 judge 返回一行 JSON 裁决，但推理模型（deepseek-v4、
# qwq 等）在吐出可见 JSON 前会烧 token 做隐藏推理——4096 覆盖我们实测过的每个
# 模型的"推理 + 裁决"；可通过 goals.judge.max_tokens 覆盖。
DEFAULT_JUDGE_MAX_TOKENS = 4096
# 发给 judge 的"最后响应 + 近期消息"最多截多少字符。
_JUDGE_RESPONSE_SNIPPET_CHARS = 4000

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)

# llm_caller 签名：(messages, temperature, max_tokens, timeout) -> 原始文本。
LLMCaller = Callable[[List[Dict[str, str]], float, int, float], str]


# ──────────────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "… [truncated]"


def _goal_judge_max_tokens() -> int:
    """解析 goals.judge.max_tokens，回退到默认值。非正/非 int 一律回退。"""
    try:
        from spirit.config import get_config_value

        value = int(get_config_value("goals.judge.max_tokens", DEFAULT_JUDGE_MAX_TOKENS))
        if value > 0:
            return value
    except Exception:
        pass
    return DEFAULT_JUDGE_MAX_TOKENS


def _goal_judge_model() -> Optional[str]:
    """解析 goals.judge.model（可选）。空 → None，调用方回退到主模型。"""
    try:
        from spirit.config import get_config_value

        m = get_config_value("goals.judge.model", "")
        m = str(m or "").strip()
        return m or None
    except Exception:
        return None


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """尽力从模型回复里抽出第一个 JSON 对象。

    与 judge 解析器共享"剥代码围栏 + 首个对象兜底"逻辑，但返回 dict（或 None）。
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _parse_judge_response(raw: str) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
    """解析 judge 的回复。输出不可用时 fail-open。

    返回 ``(verdict, reason, parse_failed, wait_directive)``：
      - ``verdict`` 是 ``"done"`` / ``"continue"`` / ``"wait"``。
      - ``parse_failed`` 为 True 表示 judge 返回了无法解释为预期 JSON 裁决的输出
        （空 body、散文、畸形 JSON）。调用方用它在你连续 N 次解析失败后自动暂停，
        免得弱 judge 模型悄悄烧光预算。
      - ``wait_directive`` 仅在 ``verdict == "wait"`` 时设置：``{"pid": int}`` /
        ``{"seconds": int}`` / ``{"session_id": str}``（judge 给了哪个就是哪个）。
        若 wait 裁决既无可用 pid 也无 seconds/session，降级为 ``continue``。

    同时接受新 ``{"verdict": ...}`` 形状和 legacy ``{"done": <bool>}`` 形状。
    """
    if not raw:
        return "continue", "judge returned empty response", True, None

    text = raw.strip()

    # 剥掉模型可能包裹 JSON 的 markdown 代码围栏。
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]

    # 第一次尝试：整块解析。
    data: Optional[Dict[str, Any]] = None
    try:
        data = json.loads(text)
    except Exception:
        # 第二次尝试：抽出第一个 JSON 对象。
        match = _JSON_OBJECT_RE.search(text)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None

    if not isinstance(data, dict):
        return "continue", f"judge reply was not JSON: {_truncate(raw, 200)!r}", True, None

    reason = str(data.get("reason") or "").strip() or "no reason provided"

    # 判定 verdict —— 优先显式 "verdict" 字段，回退到 legacy "done" 布尔。
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done_val = data.get("done")
        if isinstance(done_val, str):
            done = done_val.strip().lower() in {"true", "yes", "1", "done"}
        else:
            done = bool(done_val)
        verdict = "done" if done else "continue"

    if verdict not in {"done", "continue", "wait"}:
        verdict = "continue"

    if verdict != "wait":
        return verdict, reason, False, None

    # wait 裁决：抽出具体指令（session / pid / seconds）。接受模型可能吐的几种拼写。
    def _first_int(*keys: str) -> Optional[int]:
        for k in keys:
            v = data.get(k)
            if v is None:
                continue
            try:
                iv = int(v)
                if iv > 0:
                    return iv
            except (TypeError, ValueError):
                continue
        return None

    # 优先 session-id 指令（在进程自己的触发器——退出 OR watch-pattern 命中——时释放），
    # 其次 pid（仅退出释放），最后 seconds。
    sess = data.get("wait_on_session") or data.get("session_id") or data.get("wait_session")
    if isinstance(sess, str) and sess.strip():
        return "wait", reason, False, {"session_id": sess.strip()}
    pid = _first_int("wait_on_pid", "pid", "wait_pid")
    if pid is not None:
        return "wait", reason, False, {"pid": pid}
    seconds = _first_int("wait_for_seconds", "seconds", "wait_seconds")
    if seconds is not None:
        return "wait", reason, False, {"seconds": seconds}
    # wait 但无可用目标 —— 不能泊在空气上；当作 continue。
    return "continue", f"{reason} (wait verdict had no target — continuing)", False, None


# ──────────────────────────────────────────────────────────────────────
# 后台进程感知（wait barrier 用）
# ──────────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """pid 对应进程当前是否存活。

    Spirit 暂无 gateway.status._pid_exists，直接用 psutil；任何错误都解析为
    False（视未知为已死），这样陈旧屏障绝不会卡死循环——最坏情况是目标提前
    一轮恢复，安全。**刻意避开 ``os.kill(pid, 0)``**：它在 Windows 上不是 no-op，
    会路由到 CTRL_C_EVENT 硬杀目标的控制台进程组（bpo-14484）。
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        return False


def _session_waiting(session_id: str) -> bool:
    """泊在某个后台会话上的目标是否应继续泊车。

    接入 ``spirit.process`` 进程注册表（Phase 4.7）：会话仍在运行且尚未被
    任何人消费（wait/log/kill）时返回 True，让 goal 继续泊车等待后台进程。
    任何导入/查询异常都 fail-safe 返回 False（不泊车），以免陈旧屏障卡死循环。
    """
    if not session_id:
        return False
    try:
        from spirit.process import process_registry

        return bool(process_registry.is_session_waiting(session_id))
    except Exception:
        return False


def _render_background_block(background_processes: Optional[List[Dict[str, Any]]]) -> str:
    """为 judge prompt 渲染实时后台进程列表。

    只有 RUNNING 的进程值得展示（已退出的没什么可等）。无进程在跑时返回空串，
    使 judge prompt 与"无后台"场景逐字节一致（常见路径行为不变）。
    """
    if not background_processes:
        return ""
    lines: List[str] = []
    for p in background_processes:
        if not isinstance(p, dict):
            continue
        if p.get("status") == "exited":
            continue
        pid = p.get("pid")
        if not pid:
            continue
        cmd = _truncate(str(p.get("command") or "").replace("\n", " ").strip(), 120)
        uptime = p.get("uptime_seconds")
        tail = _truncate(str(p.get("output_preview") or "").replace("\n", " ").strip(), 120)
        sid = p.get("session_id")
        line = f"- pid {pid}"
        if sid:
            line += f" / session {sid}"
        line += f": {cmd}"
        if uptime is not None:
            line += f" (running {uptime}s)"
        wps = p.get("watch_patterns")
        if wps:
            hit = " [already matched]" if p.get("watch_hit") else ""
            line += f" | watch_patterns={wps}{hit}"
        elif p.get("notify_on_complete"):
            line += " | notify_on_complete"
        if tail:
            line += f" | recent output: {tail}"
        lines.append(line)
    if not lines:
        return ""
    return JUDGE_BACKGROUND_BLOCK_TEMPLATE.format(background_lines="\n".join(lines))


def gather_background_processes(task_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回给 goal judge 的实时后台进程快照。

    接入 ``spirit.process`` 进程注册表（Phase 4.7）：按 ``task_id`` 过滤列出
    会话（``task_id`` 为 None 时列出全部）。任何导入/查询异常都降级为 ``[]``，
    judge 退化为"无后台进程"路径（只是看不到进程，不影响 done/continue 裁决）。
    """
    try:
        from spirit.process import process_registry

        return process_registry.list_sessions(task_id=task_id)
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────
# judge 主入口
# ──────────────────────────────────────────────────────────────────────

def judge_goal(
    goal: str,
    last_response: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    timeout: float = DEFAULT_JUDGE_TIMEOUT,
    subgoals: Optional[List[str]] = None,
    background_processes: Optional[List[Dict[str, Any]]] = None,
    contract: Optional[GoalContract] = None,
) -> Tuple[str, str, bool, Optional[Dict[str, Any]]]:
    """问辅助模型：目标满足了吗。

    返回 ``(verdict, reason, parse_failed, wait_directive)``，verdict 是
    ``"done"`` / ``"continue"`` / ``"wait"`` / ``"skipped"``（judge 无法触达时）。

    ``parse_failed`` 仅在 judge 调用成功但输出不可用（空或非 JSON）时为 True。
    API/传输错误返回 False——它们是瞬时的，应静默 fail-open。

    ``llm_caller`` 是可注入的辅助模型调用器（见模块 docstring）。为 None 时
    无法裁决，fail-open 返回 ``continue``。

    刻意 fail-open：任何错误都返回 ``("continue", ..., False, None)``，坏 judge
    不会卡死进度——turn 预算和连续解析失败自动暂停是兜底。
    """
    if not goal.strip():
        return "skipped", "empty goal", False, None
    if not last_response.strip():
        # 本轮没有实质回复 —— 几乎肯定还没完成。
        return "continue", "empty response (nothing to evaluate)", False, None
    if llm_caller is None:
        return "continue", "no judge llm_caller configured", False, None

    # 构建 prompt。优先级：contract > subgoals > plain。契约和子目标同时存在时，
    # 子目标作为额外标准追加进契约块，让 judge 看到单一事实源。
    clean_subgoals = [s.strip() for s in (subgoals or []) if s and s.strip()]
    background_block = _render_background_block(background_processes)
    current_time = datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    if contract is not None and not contract.is_empty():
        contract_block = contract.render_block()
        if clean_subgoals:
            extra = "\n".join(
                f"- Extra criterion {i}: {text}"
                for i, text in enumerate(clean_subgoals, start=1)
            )
            contract_block = f"{contract_block}\n{extra}"
        prompt = JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE.format(
            goal=_truncate(goal, 2000),
            contract_block=_truncate(contract_block, 2500),
            response=_truncate(last_response, _JUDGE_RESPONSE_SNIPPET_CHARS),
            background_block=background_block,
            current_time=current_time,
        )
    elif clean_subgoals:
        subgoals_block = "\n".join(
            f"- {i}. {text}" for i, text in enumerate(clean_subgoals, start=1)
        )
        prompt = JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE.format(
            goal=_truncate(goal, 2000),
            subgoals_block=_truncate(subgoals_block, 2000),
            response=_truncate(last_response, _JUDGE_RESPONSE_SNIPPET_CHARS),
            background_block=background_block,
            current_time=current_time,
        )
    else:
        prompt = JUDGE_USER_PROMPT_TEMPLATE.format(
            goal=_truncate(goal, 2000),
            response=_truncate(last_response, _JUDGE_RESPONSE_SNIPPET_CHARS),
            background_block=background_block,
            current_time=current_time,
        )

    try:
        raw = llm_caller(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            0,  # temperature=0：裁决要确定性
            _goal_judge_max_tokens(),
            timeout,
        ) or ""
    except Exception as exc:
        logger.info("goal judge: LLM 调用失败 (%s) —— 落到 continue", exc)
        return "continue", f"judge error: {type(exc).__name__}", False, None

    verdict, reason, parse_failed, wait_directive = _parse_judge_response(raw)
    logger.info(
        "goal judge: verdict=%s reason=%s%s",
        verdict, _truncate(reason, 120),
        f" wait={wait_directive}" if wait_directive else "",
    )
    return verdict, reason, parse_failed, wait_directive


def draft_contract(
    objective: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    timeout: float = DEFAULT_JUDGE_TIMEOUT,
) -> Optional[GoalContract]:
    """把一句大白话目标扩展成结构化完成契约。

    成功返回填充好的 :class:`GoalContract`；辅助模型不可用或回复无法解析时返回
    ``None``。调用方此时回退到裸的自由目标，所以缺失/弱辅助模型绝不会阻塞设目标。
    """
    objective = (objective or "").strip()
    if not objective:
        return None
    if llm_caller is None:
        return None

    try:
        raw = llm_caller(
            [
                {"role": "system", "content": DRAFT_CONTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Objective:\n{_truncate(objective, 4000)}"},
            ],
            0,
            _goal_judge_max_tokens(),
            timeout,
        ) or ""
    except Exception as exc:
        logger.info("goal draft: LLM 调用失败 (%s)", exc)
        return None

    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        logger.debug("goal draft: 回复不是 JSON: %r", _truncate(raw, 200))
        return None
    contract = GoalContract.from_dict(data)
    return None if contract.is_empty() else contract


# ──────────────────────────────────────────────────────────────────────
# 从 SpiritAgent 构造 llm_caller
# ──────────────────────────────────────────────────────────────────────

def build_agent_llm_caller(agent) -> LLMCaller:
    """从一个 SpiritAgent 构造 judge 用的 ``llm_caller`` 闭包。

    复用 Agent 的 OpenAI 兼容 ``client`` 发起一次 side call（不是对话轮次，
    不碰消息历史、不动系统提示词，因此 prompt cache 不受影响）。judge 模型
    优先取 ``goals.judge.model`` 配置，未配置则回退到主对话模型。
    """

    def caller(
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> str:
        model = _goal_judge_model() or agent.model
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens and max_tokens > 0:
            kwargs["max_tokens"] = max_tokens
        resp = agent.client.chat.completions.create(**kwargs)
        try:
            return resp.choices[0].message.content or ""
        except Exception:
            return ""

    return caller


__all__ = [
    "judge_goal",
    "draft_contract",
    "build_agent_llm_caller",
    "gather_background_processes",
    "DEFAULT_JUDGE_TIMEOUT",
    "DEFAULT_JUDGE_MAX_TOKENS",
    "LLMCaller",
    # 供 manager 复用的内部件
    "_parse_judge_response",
    "_extract_json_object",
    "_truncate",
    "_pid_alive",
    "_session_waiting",
]
