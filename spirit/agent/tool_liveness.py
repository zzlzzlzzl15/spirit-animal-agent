"""工具活性检测 — Spirit Agent 的 liveness 检测层。

参考 Hermes ``turn_liveness.py`` (176 行) 设计，Spirit 简化版 ~150 行。

问题与动机
----------
V3 规划 R-P1.3：当前 Spirit ``tool_executor.py`` 硬编码 420s 整体超时，
单个工具卡了就一直等，没有"假死检测"。AgentBench OS 类别 420s 卡死率
100%——工具进了一个死循环 / 卡在交互式 prompt / 打开半开 socket，
就一直占着迭代预算直到 420s 才退出。

Liveness probe 设计
-------------------
1. :class:`OutputLivenessProbe` — 检查 stdout/stderr 是否有进展（"过去 N 秒
   内有新输出" → alive；否则 → 假死）
2. :class:`IntervalLivenessProbe` — 检查是否超过间隔未触发回调（适用于后台
   进程 / 长连接工具）
3. :class:`LivenessDetector` — 主类，封装 probe + 三层超时：
   - **单工具超时**（默认 60s）：单工具运行 N 秒无进展 → 取消该 future
   - **单工具软超时**（默认 30s）：warning log，不取消
   - **整体硬超时**（默认 300s）：原 420s 缩短
   - **整体软超时**（默认 90s）：warning log，不取消

集成方式
--------
``tool_executor.py`` 在每个工具 ``future.submit`` 时同时启动一个
``LivenessDetector``，超过单工具超时 → cancel future + 返回分类结果
（不是默默失败）；超过整体软超时 → warning log；超过整体硬超时 →
全局取消。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 配置 + 状态快照
# =========================================================================

@dataclass
class LivenessConfig:
    """Liveness 检测配置（三层超时）。"""

    single_tool_timeout_s: float = 60.0
    """单工具硬超时：单工具运行无进展超过这个时间 → cancel。"""

    single_tool_soft_warn_s: float = 30.0
    """单工具软超时：超过这个时间无进展 → warning log，不取消。"""

    overall_timeout_s: float = 300.0
    """整体硬超时（原 420s 缩短到 300s）：整个并发批次超过这个时间 → 全局取消。"""

    overall_soft_warn_s: float = 90.0
    """整体软超时：warning log，不取消。"""

    check_interval_s: float = 5.0
    """监控检查间隔。"""


@dataclass
class LivenessSnapshot:
    """单次活性检查的状态快照。"""

    started_at: float
    last_progress_at: float
    progress_count: int = 0
    """进度回调触发次数。"""

    last_output_bytes: int = 0
    """累计输出字节数（粗略）。"""


# =========================================================================
# Probe 抽象
# =========================================================================

class LivenessProbe:
    """活性探测抽象基类。"""

    def record_progress(self, snapshot: LivenessSnapshot, output: Any = None) -> None:
        """记录一次进度（subclass 可重写）。默认更新 last_progress_at。"""
        snapshot.last_progress_at = time.time()
        snapshot.progress_count += 1
        if isinstance(output, (bytes, str)):
            snapshot.last_output_bytes += len(output)

    def is_alive(self, snapshot: LivenessSnapshot, config: LivenessConfig) -> bool:
        """检查是否还活着。subclass 必须实现。"""
        raise NotImplementedError


class OutputLivenessProbe(LivenessProbe):
    """检查"是否有新输出"的 probe。

    适用于 terminal / search_files / read_file 这类有输出的工具：
    "过去 N 秒内有新输出" → alive；否则 → 假死。
    """

    def is_alive(self, snapshot: LivenessSnapshot, config: LivenessConfig) -> bool:
        elapsed_since_progress = time.time() - snapshot.last_progress_at
        return elapsed_since_progress < config.single_tool_timeout_s


class IntervalLivenessProbe(LivenessProbe):
    """检查心跳间隔的 probe。

    用于后台进程 / 长连接工具：依赖外部心跳回调（如 subprocess poll /
    socket 心跳）。"过去 N 秒内有进度回调" → alive。
    """

    def is_alive(self, snapshot: LivenessSnapshot, config: LivenessConfig) -> bool:
        elapsed_since_progress = time.time() - snapshot.last_progress_at
        return elapsed_since_progress < config.single_tool_timeout_s


# =========================================================================
# LivenessDetector 主类
# =========================================================================

class LivenessDetector:
    """活性检测器主类。

    包装一个长操作 + probe，提供：
    - 三层超时（单工具软/硬、整体软/硬）
    - 进度回调（每次 ``record_progress`` 重置 inactivity 计时）
    - 假死检测（超过 ``single_tool_timeout_s`` 没进度 → cancel）

    用法：
        detector = LivenessDetector(probe=OutputLivenessProbe())
        detector.start()
        try:
            for chunk in tool.run():
                detector.record_progress(output=chunk)
        finally:
            detector.stop()
            if detector.is_cancelled():
                # 工具假死
                ...
    """

    def __init__(
        self,
        probe: Optional[LivenessProbe] = None,
        config: Optional[LivenessConfig] = None,
        on_soft_warn: Optional[Callable[[float], None]] = None,
        on_hard_timeout: Optional[Callable[[float], None]] = None,
        on_single_tool_timeout: Optional[Callable[[float], None]] = None,
    ):
        self.probe = probe or OutputLivenessProbe()
        self.config = config or LivenessConfig()
        self._on_soft_warn = on_soft_warn
        self._on_hard_timeout = on_hard_timeout
        self._on_single_tool_timeout = on_single_tool_timeout

        self._snapshot: Optional[LivenessSnapshot] = None
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._cancelled = False
        self._soft_warned = False
        self._single_soft_warned = False

    def start(self) -> None:
        """启动监控线程。"""
        now = time.time()
        self._snapshot = LivenessSnapshot(
            started_at=now,
            last_progress_at=now,
        )
        self._stop_event.clear()
        self._cancelled = False
        self._soft_warned = False
        self._single_soft_warned = False
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="spirit-liveness",
        )
        self._monitor_thread.start()

    def record_progress(self, output: Any = None) -> None:
        """记录一次进度（重置 inactivity 计时）。"""
        if self._snapshot:
            self.probe.record_progress(self._snapshot, output)

    def stop(self) -> None:
        """停止监控线程（幂等）。"""
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

    def is_cancelled(self) -> bool:
        """是否已取消（liveness 触发）。"""
        return self._cancelled

    def elapsed(self) -> float:
        """已运行时间（秒）。"""
        if not self._snapshot:
            return 0.0
        return time.time() - self._snapshot.started_at

    def _monitor_loop(self) -> None:
        """后台监控循环（daemon 线程）。"""
        assert self._snapshot is not None
        while not self._stop_event.is_set():
            # 短间隔 sleep 便于响应 stop
            self._stop_event.wait(timeout=self.config.check_interval_s)
            if self._stop_event.is_set():
                break
            try:
                self._check_once()
            except Exception as exc:  # noqa: BLE001 - 监控异常不致命
                logger.debug("[liveness] 监控异常（非致命）: %s", exc)
            if self._cancelled:
                break

    def _check_once(self) -> None:
        """单次检查：三层超时判定。"""
        assert self._snapshot is not None

        elapsed = time.time() - self._snapshot.started_at
        elapsed_since_progress = time.time() - self._snapshot.last_progress_at

        # ── 1. 单工具硬超时（假死） ──
        if not self.probe.is_alive(self._snapshot, self.config):
            self._cancelled = True
            logger.warning(
                "[liveness] 单工具假死超过 %.0fs 无进展，标记取消（已运行 %.1fs）",
                self.config.single_tool_timeout_s, elapsed,
            )
            if self._on_single_tool_timeout:
                try:
                    self._on_single_tool_timeout(elapsed)
                except Exception:  # noqa: BLE001
                    pass
            return

        # ── 2. 整体硬超时 ──
        if elapsed >= self.config.overall_timeout_s:
            self._cancelled = True
            logger.warning(
                "[liveness] 整体硬超时 %.0fs，标记取消",
                self.config.overall_timeout_s,
            )
            if self._on_hard_timeout:
                try:
                    self._on_hard_timeout(elapsed)
                except Exception:  # noqa: BLE001
                    pass
            return

        # ── 3. 整体软超时（warning，只 warn 一次） ──
        if elapsed >= self.config.overall_soft_warn_s and not self._soft_warned:
            self._soft_warned = True
            logger.warning(
                "[liveness] 整体运行 %.0fs 超过软阈值 %.0fs（继续等待）",
                elapsed, self.config.overall_soft_warn_s,
            )
            if self._on_soft_warn:
                try:
                    self._on_soft_warn(elapsed)
                except Exception:  # noqa: BLE001
                    pass

        # ── 4. 单工具软超时（warning，每 30s 提示一次） ──
        if elapsed_since_progress >= self.config.single_tool_soft_warn_s:
            # 每 30s 提示一次（避免重复 warn）
            if not self._single_soft_warned or elapsed_since_progress >= self._snapshot.last_progress_at + 30.0:
                self._single_soft_warned = True
                logger.warning(
                    "[liveness] 单工具无进展 %.0fs（软阈值 %.0fs，继续等待）",
                    elapsed_since_progress, self.config.single_tool_soft_warn_s,
                )


# =========================================================================
# 工厂 + 超时结果构造
# =========================================================================

def create_default_liveness_detector(
    single_tool_timeout_s: float = 60.0,
    overall_timeout_s: float = 300.0,
    on_soft_warn: Optional[Callable[[float], None]] = None,
    on_hard_timeout: Optional[Callable[[float], None]] = None,
    on_single_tool_timeout: Optional[Callable[[float], None]] = None,
) -> LivenessDetector:
    """创建默认 liveness 检测器（OutputLivenessProbe + 默认配置）。"""
    config = LivenessConfig(
        single_tool_timeout_s=single_tool_timeout_s,
        overall_timeout_s=overall_timeout_s,
    )
    return LivenessDetector(
        probe=OutputLivenessProbe(),
        config=config,
        on_soft_warn=on_soft_warn,
        on_hard_timeout=on_hard_timeout,
        on_single_tool_timeout=on_single_tool_timeout,
    )


def build_timeout_result_json(
    tool_name: str,
    elapsed_s: float,
    timeout_kind: str = "single_tool",
    *,
    retryable: bool = True,
) -> str:
    """构造工具超时结果的 JSON 字符串（带分类信息，让 LLM 区分原因）。

    Args:
        tool_name: 工具名
        elapsed_s: 实际运行时间（秒）
        timeout_kind: ``"single_tool"`` / ``"overall"`` / ``"cancelled"``
        retryable: 是否可重试

    Returns:
        JSON 字符串，失败/超时都能被 ``classify_tool_result`` 识别为 retryable。
    """
    import json

    if timeout_kind == "cancelled":
        message = "工具执行被取消（用户中断）"
    elif timeout_kind == "overall":
        message = f"工具执行超时（整体硬超时，已运行 {elapsed_s:.1f}s）"
    else:
        message = f"工具执行超时（单工具假死，已运行 {elapsed_s:.1f}s 无进展）"

    payload = {
        "error": message,
        "status": "timeout" if timeout_kind != "cancelled" else "cancelled",
        "tool_name": tool_name,
        "elapsed_s": round(elapsed_s, 2),
        "timeout_kind": timeout_kind,
        "_classification": "retryable" if retryable else "non_retryable",
        "_retryable": retryable,
        "_guidance": (
            "这是超时/取消结果。"
            + ("可重试：换工具 / 缩短超时 / 检查网络。" if retryable else "不可重试：换方案。")
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "LivenessConfig",
    "LivenessSnapshot",
    "LivenessProbe",
    "OutputLivenessProbe",
    "IntervalLivenessProbe",
    "LivenessDetector",
    "create_default_liveness_detector",
    "build_timeout_result_json",
]
