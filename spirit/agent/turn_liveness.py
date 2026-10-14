"""Turn-level liveness watchdog — Spirit 全场景活性检测。

参考 Hermes ``agent/turn_liveness.py`` (207 行, R-P2.2 蓝图), Spirit 精简版 ~120 行。

问题与动机
----------
V3 规划 R-P2.2: 现有 ``tool_liveness.py`` 只检测"单工具执行期间的输出停滞",
但 turn 级别的"假死"场景仍裸奔:

1. **LLM 长推理卡死**: 流式响应 chunk 间 > 600s 无新字节, 思考模型拖光整轮预算
2. **tool_executor 调度卡死**: ``concurrent.futures.wait`` 内部死等, watchdog 触发不到
3. **子任务跨进程等待**: ``delegate`` 调起 worker, 父 turn 在等子节点, 中间无活动
4. **provider pool ping 阻塞**: ``llm_pool.probe`` 命中 90s socket 黑洞

活性时钟设计
------------
:class:`ActivitySnapshot` — 一次 (generation, activity_ts) 原子快照,
commit_abort 在同一锁内 revalidate generation, race-safe (Hermes 同模式).

:class:`TurnLivenessWatchdog` — 独立线程 + ``threading.Event`` 控制启停,
每 ``poll_s`` 秒采样一次; idle > ``timeout_s`` → commit abort + 写日志
+ 设置 agent 的 ``_turn_liveness_aborted`` 标志, 由 ``run_conversation``
主循环在下一次 LLM 迭代前读这个标志主动 ``break``。

集成方式
--------
``conversation_loop.run_conversation`` 在 LLM 调用前/工具执行前/子任务
checkpoint 阶段调用 ``agent._touch_activity(desc)`` 刷新时钟; watchdog
仅在 turn 活跃期间运行, turn 结束自动停止。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)


# =========================================================================
# 默认值 + 配置键
# =========================================================================

# 整体空闲超时（秒）：超过这个时间无活动 → 弹 watchdog 报警 + 准备 abort
DEFAULT_TURN_LIVENESS_TIMEOUT_S = 600.0

# 轮询间隔（秒）：watchdog 线程每 N 秒看一次活动时钟
DEFAULT_TURN_LIVENESS_POLL_S = 15.0

# 最短轮询间隔（防止配置错误把 poll 设为 0 让线程死循环）
MIN_TURN_LIVENESS_POLL_S = 0.5

# 环境变量覆盖键（用户可在 ~/.spirit/.env 或 shell 设置）
_ENV_TIMEOUT = "SPIRIT_TURN_LIVENESS_TIMEOUT_S"
_ENV_POLL = "SPIRIT_TURN_LIVENESS_POLL_S"


# =========================================================================
# 数据结构
# =========================================================================

class ActivitySnapshot(NamedTuple):
    """单次活性观察；``(generation, activity_ts)`` 由 commit_abort
    在同一锁内 revalidate 防止误杀 (Hermes #95548 同模式)."""

    generation: int
    activity_ts: Optional[float]
    idle_seconds: float


# =========================================================================
# 配置解析
# =========================================================================

def _resolve_finite_seconds(raw: Any, *, default: float, key: str) -> float:
    """容错解析一个浮点秒数: 非法 / NaN / Inf 静默回落到 default. 从不 raise."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s in config: %r — falling back to %.1f.", key, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("Invalid %s in config: %r — falling back to %.1f.", key, raw, default)
        return default
    return value


def resolve_turn_liveness_settings(
    config: Optional[Dict[str, Any]] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[float], float]:
    """从 config + env 解析 ``(timeout_s, poll_s)``.

    - ``timeout_s <= 0`` → ``None`` 表示禁用 watchdog (返回 ``None``)
    - 任何非法值 (typo / NaN / Inf / 非正 poll) 都会 warning 回落, 从不 raise

    优先级: env > config > 默认值
    """
    env = env if env is not None else __import__("os").environ

    # 1. config 提供基础值
    agent_cfg = config.get("agent") if isinstance(config, dict) else None
    section = agent_cfg.get("turn_liveness") if isinstance(agent_cfg, dict) else None
    section = section if isinstance(section, dict) else {}

    timeout_s = _resolve_finite_seconds(
        section.get("timeout_s", DEFAULT_TURN_LIVENESS_TIMEOUT_S),
        default=DEFAULT_TURN_LIVENESS_TIMEOUT_S,
        key="agent.turn_liveness.timeout_s",
    )
    poll_s = _resolve_finite_seconds(
        section.get("poll_s", DEFAULT_TURN_LIVENESS_POLL_S),
        default=DEFAULT_TURN_LIVENESS_POLL_S,
        key="agent.turn_liveness.poll_s",
    )

    # 2. env 可覆盖
    env_to = env.get(_ENV_TIMEOUT, "").strip()
    if env_to:
        timeout_s = _resolve_finite_seconds(env_to, default=timeout_s, key=_ENV_TIMEOUT)
    env_poll = env.get(_ENV_POLL, "").strip()
    if env_poll:
        poll_s = _resolve_finite_seconds(env_poll, default=poll_s, key=_ENV_POLL)

    # 3. 钳制 + 语义解释
    if poll_s <= 0:
        logger.warning("Invalid poll_s=%r, falling back to %.1f", poll_s, DEFAULT_TURN_LIVENESS_POLL_S)
        poll_s = DEFAULT_TURN_LIVENESS_POLL_S
    poll_s = max(MIN_TURN_LIVENESS_POLL_S, poll_s)
    if timeout_s <= 0:
        timeout_s = None  # 0 = opt-out
    return timeout_s, poll_s


# =========================================================================
# Watchdog 主类
# =========================================================================

class TurnLivenessWatchdog:
    """turn 级别活性 watchdog. 一次 turn 绑定一个实例, 启动线程轮询.

    触发顺序（Hermes #95548 同款语义）::

        sample (锁内读 activity_ts)
          → idle >= timeout?
            → surface_stall (log + 写 _last_surfaced_generation, 限流)
            → commit_abort (回调; 业务层写 _turn_liveness_aborted)
              → 成功？ → deactivate_turn (回调, 释放会话占用)
                       → surface_committed_abort (最终 log)
              → 失败？ → 不做事, 下个 poll 继续观察

    设计取舍
    - 不用 ``agent.periodic_scheduler`` (Hermes-only); Spirit 用独立线程
      + ``threading.Event`` 启停, 启动 / 停止确定性更强, 错误隔离.
    - 所有回调 ``getattr(agent, name, default)`` 防御性读取, 注入方缺失
      接口时 watchdog 仍能跑 (降级为"只 log").
    - 一次 commit 失败不会让 watchdog 死, 下次 poll 重新尝试 (race-safe).
    """

    def __init__(
        self,
        agent: Any,
        *,
        timeout_s: Optional[float],
        poll_s: float,
    ) -> None:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0 or None (got {timeout_s!r})")
        self._agent = agent
        self._timeout_s = timeout_s
        self._poll_s = max(MIN_TURN_LIVENESS_POLL_S, float(poll_s))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_surfaced_generation: Optional[int] = None

    # ---- 生命周期 ----

    @property
    def enabled(self) -> bool:
        """是否启用（timeout_s=None 表示 opt-out）."""
        return self._timeout_s is not None

    def start(self) -> None:
        """启动 watchdog 线程. 已启动时幂等."""
        if not self.enabled:
            logger.debug("[R-P2.2] Turn liveness watchdog disabled (timeout_s is None)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        sess = agent_ref_session(self._agent)
        self._thread = threading.Thread(
            target=self._loop,
            name=f"turn-liveness-{sess[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.debug("[R-P2.2] Turn liveness watchdog started: timeout=%.1fs poll=%.1fs",
                     self._timeout_s, self._poll_s)

    def stop(self, timeout: float = 2.0) -> None:
        """停止 watchdog 线程. 已停止时幂等."""
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    # ---- 主循环 ----

    def _loop(self) -> None:
        """watchdog 主循环. 每个 poll_s 秒跑一次 _tick."""
        while not self._stop_event.is_set():
            # Event.wait 在 stop() 时立即返回, 比 sleep 干净
            if self._stop_event.wait(self._poll_s):
                return
            try:
                self._tick()
            except Exception as exc:
                logger.warning("[R-P2.2] watchdog tick 异常, 继续: %s", exc, exc_info=True)

    def _tick(self) -> None:
        """单次采样. idle 超阈值 → commit abort."""
        snapshot = self._sample()
        if snapshot is None:
            return  # turn 已结束
        if snapshot.idle_seconds < (self._timeout_s or 0):
            return
        # 1. 观测 log (限流 per generation)
        self._surface_stall(snapshot)
        # 2. commit abort (回调; 业务层写 _turn_liveness_aborted)
        if not self._commit_abort(snapshot):
            return
        # 3. deactivate (回调, 释放会话占用 / 通知上层)
        self._deactivate_turn()
        # 4. 最终 log
        self._surface_committed_abort(snapshot)

    def _sample(self) -> Optional[ActivitySnapshot]:
        """原子采样 (generation, activity_ts). turn 已结束 → None."""
        agent = self._agent
        if not self._is_turn_active():
            return None
        activity_lock = getattr(agent, "_activity_lock", None)
        if activity_lock is None:
            # 没有锁 → 防御性退化: 直接读 (race 在 turn 内部不重要)
            return self._sample_unsafe()
        with activity_lock:
            return self._sample_unsafe()

    def _sample_unsafe(self) -> Optional[ActivitySnapshot]:
        agent = self._agent
        generation = getattr(agent, "_turn_liveness_activity_generation", 0)
        activity_ts = getattr(agent, "_last_activity_ts", None)
        if activity_ts is None:
            return ActivitySnapshot(generation, None, 0.0)
        idle = max(0.0, time.time() - activity_ts)
        return ActivitySnapshot(generation, activity_ts, idle)

    # ---- 业务回调（注入方决定语义） ----

    def _is_turn_active(self) -> bool:
        """判定 turn 是否仍在跑. 默认从 ``agent._turn_active`` 读."""
        return bool(getattr(self._agent, "_turn_active", False))

    def _commit_abort(self, snapshot: ActivitySnapshot) -> bool:
        """业务层 commit. 默认从 ``agent._commit_turn_liveness_abort`` 读.

        返回 True 表示 abort 已被记录 (e.g. 已设 ``_turn_liveness_aborted``),
        False 表示业务方认为不需要 abort (例如 generation 已变化 = 已恢复).
        """
        commit = getattr(self._agent, "_commit_turn_liveness_abort", None)
        if not callable(commit):
            return False
        try:
            return bool(commit(snapshot, f"turn idle {int(snapshot.idle_seconds)}s"))
        except Exception as exc:
            logger.debug("[R-P2.2] commit_abort 异常: %s", exc)
            return False

    def _deactivate_turn(self) -> None:
        """deactivate turn 释放资源. 默认从 ``agent._deactivate_turn`` 读."""
        fn = getattr(self._agent, "_deactivate_turn", None)
        if not callable(fn):
            return
        try:
            fn()
        except Exception as exc:
            logger.debug("[R-P2.2] deactivate 异常: %s", exc)

    # ---- 日志 / 限制 ----

    def _surface_stall(self, snapshot: ActivitySnapshot) -> None:
        """观测 log: 限流 per generation, 避免反复拒绝 abort 时每 poll 都刷屏."""
        if self._last_surfaced_generation == snapshot.generation:
            return
        self._last_surfaced_generation = snapshot.generation
        session_id = agent_ref_session(self._agent)
        logger.warning(
            "[R-P2.2] turn 活性告警: session=%s idle=%.1fs (last: %r). "
            "正在 commit abort (下一轮主循环会主动 break).",
            session_id, snapshot.idle_seconds,
            getattr(self._agent, "_last_activity_desc", None),
        )

    def _surface_committed_abort(self, snapshot: ActivitySnapshot) -> None:
        """abort 已 commit 的最终 log. 仅在 commit 成功后调用一次.

        v0.3.5: 降级为 logger.info — watchdog 强制结束 turn 是设计内的资源回收机制
        （idle 超时或 stall 保护），不是异常。CLI 默认 level=ERROR 下不会被渲染到
        终端，避免用户以为是 crash。verbose 模式 (DEBUG) 下仍能看到。
        """
        session_id = agent_ref_session(self._agent)
        logger.info(
            "[R-P2.2] turn 已被 watchdog 强制结束: session=%s idle=%.1fs. "
            "run_conversation 主循环将在下个检查点 break.",
            session_id, snapshot.idle_seconds,
        )


def agent_ref_session(agent: Any) -> str:
    """小工具: 拿 session_id, 缺失时返回 '<no-session>'."""
    return getattr(agent, "session_id", None) or "<no-session>"
