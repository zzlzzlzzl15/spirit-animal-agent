"""Ralph Loop 的 prompt 模板集合。

参考 Hermes ``hermes_cli/goals.py`` 的模板，逐字对齐（prompt 正文保持英文——
发给 LLM 的裁决/续传指令用英文更稳定，也与 Hermes 行为一致；仅注释用中文）。

三类模板：

1. **续传 prompt**（CONTINUATION_*）——judge 判定"未完成"后，作为一条普通
   user 消息喂回同一会话，踢 Agent 继续朝目标推进。有三种变体：纯自由目标 /
   带完成契约 / 带用户中途追加的子目标。
2. **judge prompt**（JUDGE_*）——每轮结束后问辅助模型"目标达成了吗"，返回
   done / continue / wait 三态裁决（单行 JSON）。
3. **契约起草 prompt**（DRAFT_CONTRACT_SYSTEM_PROMPT）——把一句大白话目标
   扩展成结构化完成契约供用户过目。
"""

from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────
# 续传 prompt（judge 判定未完成后喂回会话）
# ──────────────────────────────────────────────────────────────────────

# 纯自由目标（无契约、无子目标）的续传。
CONTINUATION_PROMPT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Continue working toward this goal. Take the next concrete step. "
    "If you believe the goal is complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly and stop."
)

# 目标带结构化完成契约时使用。契约块告诉 Agent "done"到底指什么、如何证明、
# 不能破坏什么、范围在哪、何时该停下问人——让它瞄准验证面而不是含糊地宣告胜利。
CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Completion contract:\n"
    "{contract_block}\n\n"
    "Continue working toward the outcome above. Take the next concrete step. "
    "Stay within the stated boundaries and do not violate the constraints. "
    "Before claiming the goal is done, satisfy the Verification criterion and "
    "show the concrete evidence (command output, file contents, test result). "
    "If you hit the stated stop condition or are otherwise blocked and need "
    "user input, say so clearly and stop."
)

# 用户通过 /subgoal 追加了一条或多条验收标准时使用。原样呈现给 Agent，让它
# 知道下一轮该瞄准什么；也呈现给 judge，让裁决把它们一并考虑。
CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "[Continuing toward your standing goal]\n"
    "Goal: {goal}\n\n"
    "Additional criteria the user added mid-loop:\n"
    "{subgoals_block}\n\n"
    "Continue working toward the goal AND all additional criteria. Take "
    "the next concrete step. If you believe the goal and every "
    "additional criterion are complete, state so explicitly and stop. "
    "If you are blocked and need input from the user, say so clearly "
    "and stop."
)


# ──────────────────────────────────────────────────────────────────────
# judge prompt（辅助模型裁决 done / continue / wait）
# ──────────────────────────────────────────────────────────────────────

JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has "
    "achieved a user's stated goal. You receive the goal text, the agent's "
    "most recent response, and — when present — a list of background "
    "processes the agent has running. Decide one of three verdicts.\n\n"
    "DONE — the goal is fully satisfied:\n"
    "- The response explicitly confirms the goal was completed, OR\n"
    "- The response clearly shows the final deliverable was produced, OR\n"
    "- The response explains the goal is unachievable / blocked / needs "
    "user input (treat this as DONE with reason describing the block).\n\n"
    "WAIT — the goal is NOT done, but the next step is to wait for async "
    "work to finish rather than act again. Choose this ONLY when the agent's "
    "progress is genuinely gated on something running on its own:\n"
    "- A background process listed below is still running AND the response "
    "shows the agent is waiting on its result (e.g. a CI poller, build, "
    "test run, deploy). If the process has a session id, return it in "
    "``wait_on_session`` — that releases when the process exits OR its "
    "watch_patterns trigger fires (use this for a long-lived watcher that "
    "signals mid-run and may never exit). Otherwise return its pid in "
    "``wait_on_pid`` (releases on exit only).\n"
    "- The agent says it is rate-limited / backing off / must wait a fixed "
    "period — return seconds in ``wait_for_seconds``.\n"
    "Picking WAIT parks the loop without burning a turn; it resumes "
    "automatically when the pid exits or the time elapses. Do NOT pick WAIT "
    "just because work remains — only when re-poking now would be pure "
    "busy-work because the agent can't progress until the async thing "
    "finishes.\n\n"
    "CONTINUE — not done, and there is a concrete next step the agent can "
    "take right now. This is the default when in doubt.\n\n"
    "Reply ONLY with a single JSON object on one line. Shapes:\n"
    '{"verdict": "done", "reason": "<one sentence>"}\n'
    '{"verdict": "continue", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_session": "<id>", "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_on_pid": <int>, "reason": "<one sentence>"}\n'
    '{"verdict": "wait", "wait_for_seconds": <int>, "reason": "<one sentence>"}\n'
    "The legacy shape {\"done\": <true|false>, \"reason\": \"...\"} is still "
    "accepted (true=done, false=continue)."
)


# 当 Agent 有后台进程在跑时渲染进 judge prompt。给 judge 决定 WAIT vs CONTINUE
# （以及等哪个 pid）所需的上下文，无需它自己去探测。
JUDGE_BACKGROUND_BLOCK_TEMPLATE = (
    "Background processes the agent currently has running (it may be waiting "
    "on one of these):\n{background_lines}\n\n"
)


JUDGE_USER_PROMPT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Is the goal satisfied — done, continue, or wait?"
)

# 用户追加了 /subgoal 标准时使用。judge 必须评估全部标准都满足，而不只是原目标。
JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Additional criteria the user added mid-loop (all must also be "
    "satisfied for the goal to be DONE):\n{subgoals_block}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision: For each numbered criterion above, find concrete "
    "evidence in the agent's response that the criterion is "
    "satisfied. Do not accept generic phrases like 'all requirements "
    "met' or 'implying it was done' — require specific evidence (a "
    "file contents excerpt, an output line, a command result). If "
    "ANY criterion lacks specific evidence in the response, the goal "
    "is NOT done — return CONTINUE (or WAIT if blocked on a listed "
    "background process).\n\n"
    "Is the goal AND every additional criterion satisfied?"
)


# 目标带结构化完成契约时使用。judge 严格依据 Verification 标准裁决 DONE，
# 并在约束被违反时拒绝接受完成。
JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE = (
    "Goal:\n{goal}\n\n"
    "Completion contract (the authoritative definition of done):\n"
    "{contract_block}\n\n"
    "Agent's most recent response:\n{response}\n\n"
    "{background_block}"
    "Current time: {current_time}\n\n"
    "Decision rules:\n"
    "- The goal is DONE only when the Verification criterion is satisfied AND "
    "the response shows concrete evidence of it (a command result, file "
    "contents excerpt, test/benchmark output) — not a claim like 'done' or "
    "'all tests pass' without evidence.\n"
    "- If any stated Constraint was violated, the goal is NOT done — CONTINUE.\n"
    "- If the response shows the agent is waiting on a listed background "
    "process to satisfy the Verification criterion (e.g. CI is the "
    "verification and it's still running), return WAIT on that process "
    "instead of re-poking — re-poking now would be pure busy-work.\n"
    "- If the response explains the work is blocked / unachievable / needs "
    "user input (e.g. the stated Stop condition was hit), treat it as DONE "
    "with the reason describing the block.\n"
    "- Otherwise the goal is NOT done — CONTINUE.\n\n"
    "Is the goal satisfied per its completion contract — done, continue, or wait?"
)


# ──────────────────────────────────────────────────────────────────────
# 契约起草 prompt（/goal draft：大白话目标 → 结构化契约）
# ──────────────────────────────────────────────────────────────────────

DRAFT_CONTRACT_SYSTEM_PROMPT = (
    "You turn a user's plain-language objective into a structured completion "
    "contract for an autonomous coding agent. The contract has five fields:\n"
    "- outcome: the single end state that must be true when done\n"
    "- verification: the specific test / command / artifact that PROVES the "
    "outcome (must be concrete and checkable)\n"
    "- constraints: what must NOT change or regress\n"
    "- boundaries: which files, dirs, tools, or systems are in scope\n"
    "- stop_when: the condition under which the agent should stop and ask "
    "for human input instead of pushing on\n\n"
    "Infer sensible, specific values from the objective and any project "
    "context implied by it. Prefer concrete verification (a named test "
    "command, a build, a benchmark) over vague phrases. Keep each field to "
    "one or two sentences. If a field genuinely cannot be inferred, use an "
    "empty string for it.\n\n"
    "Reply ONLY with a single JSON object on one line:\n"
    '{"outcome": "...", "verification": "...", "constraints": "...", '
    '"boundaries": "...", "stop_when": "..."}'
)


__all__ = [
    "CONTINUATION_PROMPT_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE",
    "CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE",
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_BACKGROUND_BLOCK_TEMPLATE",
    "JUDGE_USER_PROMPT_TEMPLATE",
    "JUDGE_USER_PROMPT_WITH_SUBGOALS_TEMPLATE",
    "JUDGE_USER_PROMPT_WITH_CONTRACT_TEMPLATE",
    "DRAFT_CONTRACT_SYSTEM_PROMPT",
]
