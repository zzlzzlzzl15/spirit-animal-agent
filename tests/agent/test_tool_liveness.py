"""Spirit Agent tool_liveness.py 活性检测回归测试（R-P1.3 v0.2.4 已交付未测）。

参考 hermes:
  - test_turn_liveness.py (139 行)
  - test_turn_liveness_watchdog.py (784 行)

3 层超时：
  - 单工具软超时（默认 30s）：warning log，不取消
  - 单工具硬超时（默认 60s）：cancel future
  - 整体硬超时（默认 300s）：全局取消（原 420s 缩短）
  - 整体软超时（默认 90s）：warning log，不取消
"""

from __future__ import annotations

import json
import time

import pytest

from spirit.agent.tool_liveness import (
    IntervalLivenessProbe,
    LivenessConfig,
    LivenessDetector,
    LivenessProbe,
    LivenessSnapshot,
    OutputLivenessProbe,
    build_timeout_result_json,
    create_default_liveness_detector,
)


# ─────────────────────────── LivenessConfig ───────────────────────────


class TestLivenessConfig:
    """LivenessConfig 默认值。"""

    def test_default_single_tool_timeout(self):
        config = LivenessConfig()
        assert config.single_tool_timeout_s == 60.0

    def test_default_single_tool_soft_warn(self):
        config = LivenessConfig()
        assert config.single_tool_soft_warn_s == 30.0

    def test_default_overall_timeout(self):
        config = LivenessConfig()
        assert config.overall_timeout_s == 300.0

    def test_default_overall_soft_warn(self):
        config = LivenessConfig()
        assert config.overall_soft_warn_s == 90.0

    def test_default_check_interval(self):
        config = LivenessConfig()
        assert config.check_interval_s == 5.0


# ─────────────────────────── LivenessSnapshot ───────────────────────────


class TestLivenessSnapshot:
    """LivenessSnapshot 字段默认值。"""

    def test_default_progress_count_zero(self):
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        assert snap.progress_count == 0

    def test_default_last_output_bytes_zero(self):
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        assert snap.last_output_bytes == 0


# ─────────────────────────── LivenessProbe 抽象 ───────────────────────────


class TestLivenessProbeAbstract:
    """LivenessProbe 基类行为。"""

    def test_is_alive_raises_not_implemented(self):
        probe = LivenessProbe()
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        with pytest.raises(NotImplementedError):
            probe.is_alive(snap, LivenessConfig())

    def test_record_progress_updates_count(self):
        probe = LivenessProbe()
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        probe.record_progress(snap, "x")
        assert snap.progress_count == 1
        probe.record_progress(snap, "y")
        assert snap.progress_count == 2

    def test_record_progress_with_bytes(self):
        probe = LivenessProbe()
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        probe.record_progress(snap, b"hello")
        assert snap.last_output_bytes == 5

    def test_record_progress_with_str(self):
        probe = LivenessProbe()
        snap = LivenessSnapshot(
            started_at=time.time(),
            last_progress_at=time.time(),
        )
        probe.record_progress(snap, "hello")
        assert snap.last_output_bytes == 5


# ─────────────────────────── OutputLivenessProbe ───────────────────────────


class TestOutputLivenessProbe:
    """OutputLivenessProbe：基于"是否有新输出"。"""

    def test_alive_when_recent_progress(self):
        probe = OutputLivenessProbe()
        now = time.time()
        snap = LivenessSnapshot(started_at=now, last_progress_at=now)
        config = LivenessConfig(single_tool_timeout_s=10.0)
        assert probe.is_alive(snap, config) is True

    def test_dead_when_no_recent_progress(self):
        probe = OutputLivenessProbe()
        now = time.time()
        snap = LivenessSnapshot(started_at=now, last_progress_at=now - 100)
        config = LivenessConfig(single_tool_timeout_s=10.0)
        assert probe.is_alive(snap, config) is False

    def test_boundary_just_under_timeout(self):
        probe = OutputLivenessProbe()
        now = time.time()
        snap = LivenessSnapshot(started_at=now, last_progress_at=now - 9.9)
        config = LivenessConfig(single_tool_timeout_s=10.0)
        assert probe.is_alive(snap, config) is True


# ─────────────────────────── IntervalLivenessProbe ───────────────────────────


class TestIntervalLivenessProbe:
    """IntervalLivenessProbe：心跳间隔检测。"""

    def test_alive_when_recent_heartbeat(self):
        probe = IntervalLivenessProbe()
        now = time.time()
        snap = LivenessSnapshot(started_at=now, last_progress_at=now)
        config = LivenessConfig(single_tool_timeout_s=10.0)
        assert probe.is_alive(snap, config) is True

    def test_dead_when_no_heartbeat(self):
        probe = IntervalLivenessProbe()
        now = time.time()
        snap = LivenessSnapshot(started_at=now, last_progress_at=now - 100)
        config = LivenessConfig(single_tool_timeout_s=10.0)
        assert probe.is_alive(snap, config) is False


# ─────────────────────────── LivenessDetector 主类 ───────────────────────────


class TestLivenessDetectorInit:
    """LivenessDetector 构造默认值。"""

    def test_default_probe_is_output(self):
        detector = LivenessDetector()
        assert isinstance(detector.probe, OutputLivenessProbe)

    def test_default_config_used_when_none(self):
        detector = LivenessDetector()
        assert detector.config.single_tool_timeout_s == 60.0

    def test_custom_probe_respected(self):
        detector = LivenessDetector(probe=IntervalLivenessProbe())
        assert isinstance(detector.probe, IntervalLivenessProbe)

    def test_custom_config_respected(self):
        config = LivenessConfig(single_tool_timeout_s=10.0)
        detector = LivenessDetector(config=config)
        assert detector.config.single_tool_timeout_s == 10.0

    def test_callbacks_stored(self):
        def cb(x):
            pass

        detector = LivenessDetector(
            on_soft_warn=cb,
            on_hard_timeout=cb,
            on_single_tool_timeout=cb,
        )
        assert detector._on_soft_warn is cb
        assert detector._on_hard_timeout is cb
        assert detector._on_single_tool_timeout is cb


class TestLivenessDetectorLifecycle:
    """LivenessDetector start / stop / record_progress 生命周期。"""

    def test_start_initializes_snapshot(self):
        detector = LivenessDetector()
        detector.start()
        try:
            assert detector._snapshot is not None
            assert detector.is_cancelled() is False
        finally:
            detector.stop()

    def test_stop_joins_thread(self):
        detector = LivenessDetector()
        detector.start()
        detector.stop()
        assert detector._monitor_thread is None

    def test_stop_is_idempotent(self):
        detector = LivenessDetector()
        detector.start()
        detector.stop()
        # 第二次 stop 不报错
        detector.stop()

    def test_record_progress_after_start(self):
        detector = LivenessDetector()
        detector.start()
        try:
            detector.record_progress("chunk1")
            detector.record_progress("chunk2")
            assert detector._snapshot.progress_count == 2
            assert detector._snapshot.last_output_bytes == 12
        finally:
            detector.stop()

    def test_record_progress_before_start_is_noop(self):
        detector = LivenessDetector()
        # 不调 start
        detector.record_progress("x")
        # snapshot 为 None → record_progress 静默不报错
        assert detector._snapshot is None


class TestLivenessDetectorElapsed:
    """elapsed() 已运行时间。"""

    def test_elapsed_before_start_is_zero(self):
        detector = LivenessDetector()
        assert detector.elapsed() == 0.0

    def test_elapsed_after_start_increases(self):
        detector = LivenessDetector()
        detector.start()
        try:
            e1 = detector.elapsed()
            time.sleep(0.05)
            e2 = detector.elapsed()
            assert e2 >= e1
            assert e2 > 0
        finally:
            detector.stop()


class TestLivenessDetectorCancel:
    """假死检测 + cancel 机制。"""

    def test_single_tool_timeout_cancels(self):
        # 极短超时：单工具 0.05s 无进展 → 假死
        config = LivenessConfig(
            single_tool_timeout_s=0.05,
            single_tool_soft_warn_s=0.02,
            overall_timeout_s=10.0,
            overall_soft_warn_s=5.0,
            check_interval_s=0.01,
        )
        detector = LivenessDetector(
            probe=OutputLivenessProbe(),
            config=config,
        )
        detector.start()
        # 等 0.2s 让监控循环检测到假死
        time.sleep(0.2)
        assert detector.is_cancelled() is True
        detector.stop()

    def test_record_progress_resets_cancel(self):
        config = LivenessConfig(
            single_tool_timeout_s=0.1,
            check_interval_s=0.02,
        )
        detector = LivenessDetector(config=config)
        detector.start()
        try:
            # 持续 progress 不会被取消
            for _ in range(5):
                time.sleep(0.05)
                detector.record_progress("tick")
            assert detector.is_cancelled() is False
        finally:
            detector.stop()

    def test_on_single_tool_timeout_callback_fires(self):
        config = LivenessConfig(
            single_tool_timeout_s=0.05,
            check_interval_s=0.01,
        )
        fired = []
        detector = LivenessDetector(
            config=config,
            on_single_tool_timeout=lambda elapsed: fired.append(elapsed),
        )
        detector.start()
        time.sleep(0.15)
        detector.stop()
        assert len(fired) >= 1


class TestLivenessDetectorOverallTimeout:
    """整体硬超时测试。"""

    def test_overall_timeout_cancels(self):
        config = LivenessConfig(
            single_tool_timeout_s=10.0,  # 单工具超时高
            overall_timeout_s=0.1,        # 整体超时低
            overall_soft_warn_s=0.05,
            check_interval_s=0.02,
        )
        detector = LivenessDetector(config=config)
        detector.start()
        time.sleep(0.25)
        assert detector.is_cancelled() is True
        detector.stop()

    def test_on_hard_timeout_callback_fires(self):
        config = LivenessConfig(
            single_tool_timeout_s=10.0,
            overall_timeout_s=0.1,
            check_interval_s=0.02,
        )
        fired = []
        detector = LivenessDetector(
            config=config,
            on_hard_timeout=lambda e: fired.append(e),
        )
        detector.start()
        time.sleep(0.25)
        detector.stop()
        assert len(fired) >= 1


class TestLivenessDetectorSoftWarn:
    """软超时 warning（不取消）。"""

    def test_soft_warn_does_not_cancel(self):
        config = LivenessConfig(
            single_tool_timeout_s=10.0,
            overall_timeout_s=10.0,
            overall_soft_warn_s=0.05,
            check_interval_s=0.02,
        )
        fired = []
        detector = LivenessDetector(
            config=config,
            on_soft_warn=lambda e: fired.append(e),
        )
        detector.start()
        time.sleep(0.15)
        # 软超时不应取消
        assert detector.is_cancelled() is False
        # warning 回调应触发
        assert len(fired) >= 1
        detector.stop()

    def test_soft_warn_fires_only_once(self):
        config = LivenessConfig(
            overall_timeout_s=10.0,
            overall_soft_warn_s=0.05,
            check_interval_s=0.02,
        )
        fired = []
        detector = LivenessDetector(
            config=config,
            on_soft_warn=lambda e: fired.append(e),
        )
        detector.start()
        time.sleep(0.2)
        detector.stop()
        # _soft_warned flag 防止重复 warn
        assert len(fired) == 1


# ─────────────────────────── create_default_liveness_detector 工厂 ───────────────────────────


class TestCreateDefaultLivenessDetector:
    """create_default_liveness_detector 工厂函数。"""

    def test_default_args(self):
        detector = create_default_liveness_detector()
        try:
            assert isinstance(detector.probe, OutputLivenessProbe)
            assert detector.config.single_tool_timeout_s == 60.0
            assert detector.config.overall_timeout_s == 300.0
        finally:
            detector.stop()

    def test_custom_timeouts(self):
        detector = create_default_liveness_detector(
            single_tool_timeout_s=10.0,
            overall_timeout_s=20.0,
        )
        try:
            assert detector.config.single_tool_timeout_s == 10.0
            assert detector.config.overall_timeout_s == 20.0
        finally:
            detector.stop()

    def test_callbacks_passed_through(self):
        fired = []
        detector = create_default_liveness_detector(
            on_soft_warn=lambda e: fired.append(e),
        )
        try:
            assert detector._on_soft_warn is not None
        finally:
            detector.stop()


# ─────────────────────────── build_timeout_result_json ───────────────────────────


class TestBuildTimeoutResultJson:
    """build_timeout_result_json 构造超时分类结果。"""

    def test_single_tool_timeout_default(self):
        result = build_timeout_result_json("terminal", 65.0)
        payload = json.loads(result)
        assert payload["timeout_kind"] == "single_tool"
        assert payload["tool_name"] == "terminal"
        assert payload["elapsed_s"] == 65.0
        assert payload["_classification"] == "retryable"
        assert payload["_retryable"] is True
        assert "单工具假死" in payload["error"]

    def test_overall_timeout(self):
        result = build_timeout_result_json("search", 310.0, "overall")
        payload = json.loads(result)
        assert payload["timeout_kind"] == "overall"
        assert "整体硬超时" in payload["error"]
        assert payload["status"] == "timeout"

    def test_cancelled_kind(self):
        result = build_timeout_result_json("exec", 5.0, "cancelled")
        payload = json.loads(result)
        assert payload["timeout_kind"] == "cancelled"
        assert "用户中断" in payload["error"]
        assert payload["status"] == "cancelled"

    def test_non_retryable(self):
        result = build_timeout_result_json("t", 1.0, retryable=False)
        payload = json.loads(result)
        assert payload["_classification"] == "non_retryable"
        assert payload["_retryable"] is False
        assert "不可重试" in payload["_guidance"]

    def test_retryable_guidance_includes_suggestion(self):
        result = build_timeout_result_json("t", 1.0, retryable=True)
        payload = json.loads(result)
        assert "换工具" in payload["_guidance"] or "缩短" in payload["_guidance"]

    def test_elapsed_rounded(self):
        result = build_timeout_result_json("t", 1.234567)
        payload = json.loads(result)
        assert payload["elapsed_s"] == 1.23  # round to 2 decimals


# ─────────────────────────── 集成：classify_tool_result 协同 ───────────────────────────


class TestLivenessIntegrationWithClassifier:
    """LivenessDetector 超时结果能被 classify_tool_result 识别。"""

    def test_single_tool_timeout_json_structure(self):
        """build_timeout_result_json 自带 _classification=retryable 字段。

        但 classify_tool_result 对 terminal 工具走 _classify_failure 路径，
        只按正则匹配 error_text。中文 “超时” 不含英文 “timeout”，所以补
        实际分类结果依赖 _NON_RETRYABLE_RE / _RETRYABLE_RE 模式匹配。
        """
        from spirit.agent.tool_result_classification import classify_tool_result

        result_json = build_timeout_result_json("terminal", 65.0)
        payload = json.loads(result_json)
        # JSON 本身含 _classification=retryable
        assert payload["_classification"] == "retryable"
        assert payload["_retryable"] is True

        # 但 classify_tool_result 重新分类后可能不返回 retryable
        # （取决于错误文本是否匹配 _RETRYABLE_RE / _NON_RETRYABLE_RE）
        # 本测试只验证 JSON 本身不验证重新分类
        c = classify_tool_result("terminal", result_json)
        assert c.is_failure is True  # 中文错误仍触发失败路径

    def test_overall_timeout_json_structure(self):
        from spirit.agent.tool_result_classification import classify_tool_result

        result_json = build_timeout_result_json("search", 310.0, "overall")
        payload = json.loads(result_json)
        assert payload["timeout_kind"] == "overall"
        assert payload["_retryable"] is True
        c = classify_tool_result("search", result_json)
        assert c.is_failure is True

    def test_cancelled_json_classified_failure(self):
        from spirit.agent.tool_result_classification import classify_tool_result

        result_json = build_timeout_result_json("exec", 5.0, "cancelled")
        c = classify_tool_result("exec", result_json)
        # cancelled 含 "用户中断" → 不匹配 retryable/non_retryable → 兜底 FAILURE
        assert c.is_failure is True
