"""金融量化领域示例 —— Spirit Agent Phase 6.F（docs/13 §F2）。

演示如何把自进化闭环接到具体领域（**仅为示例，框架领域无关**）：

- **信息收集**：注入式 ``data_source`` 提供行情/新闻（无真实 API 依赖，测试可打桩）；
- **任务生成**：从标的池自动派"因子/策略探索"任务（自派任务，不靠人工）；
- **验证器**：复用 :class:`~spirit.evolution.verifier.MetricThresholdVerifier` 做回测夏普
  阈值裁决（**客观信号 > 模型自评**，docs/13 §6.2）；
- **奖励信号**：回测夏普比率，可客观计算（docs/13 §九.5）。

所有外部依赖（数据源 / 回测器）均**注入式**，故本模块离线可测。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from spirit.evolution.domains.base import Domain, DomainInfo
from spirit.evolution.protocol import Verdict
from spirit.evolution.verifier import Evidence, MetricThresholdVerifier

logger = logging.getLogger(__name__)

# 数据源：无参，返回 {"symbols": [...], "news": [...], ...}
DataSource = Callable[[], Dict[str, Any]]
# 回测器：(task, data) -> {"sharpe": float, "return": float, ...}
BacktestFn = Callable[[str, Dict[str, Any]], Dict[str, float]]


class FinanceDomain(Domain):
    """金融量化领域适配器（示例）。"""

    name = "finance"

    def __init__(
        self,
        *,
        data_source: Optional[DataSource] = None,
        backtest: Optional[BacktestFn] = None,
        sharpe_threshold: float = 1.0,
        symbols: Optional[List[str]] = None,
        metric: str = "sharpe",
    ) -> None:
        self._data_source = data_source
        self._backtest = backtest
        self.sharpe_threshold = sharpe_threshold
        self.symbols = list(symbols or [])
        self.metric = metric

    # -- 信息收集 ----------------------------------------------------------
    def collect_info(self) -> DomainInfo:
        """调用注入的数据源收集行情/新闻；无数据源或异常 → 空信息（fail-soft）。"""
        data: Dict[str, Any] = {}
        if callable(self._data_source):
            try:
                raw = self._data_source()
                if isinstance(raw, dict):
                    data = raw
            except Exception as exc:
                logger.warning("evolution.finance: 数据源采集失败: %s", exc)
        symbols = list(data.get("symbols") or self.symbols)
        news = data.get("news") or []
        summary = f"标的 {len(symbols)} 个" + (f"，新闻 {len(news)} 条" if news else "")
        return DomainInfo(
            domain=self.name, data=data, summary=summary, collected_at=time.time()
        )

    # -- 任务生成（自派任务）----------------------------------------------
    def propose_tasks(self, info: Optional[DomainInfo] = None, *, limit: int = 3) -> List[str]:
        """从标的池派"因子/策略探索 + 回测"任务。"""
        symbols: List[str] = []
        if info and info.data:
            symbols = list(info.data.get("symbols") or [])
        if not symbols:
            symbols = list(self.symbols)
        tasks: List[str] = []
        for sym in symbols[: max(0, limit)]:
            tasks.append(
                f"为 {sym} 构建动量因子策略并回测（要求 {self.metric} ≥ {self.sharpe_threshold}）"
            )
        return tasks

    # -- 客观验证（复用 MetricThresholdVerifier）---------------------------
    def verifier_for(self, task: str) -> MetricThresholdVerifier:
        return MetricThresholdVerifier(metric=self.metric, min_value=self.sharpe_threshold)

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        """基于回测指标客观裁决。无指标 → UNVERIFIED（数据缺失，绝不当 PASS/FAIL）。"""
        if isinstance(evidence, dict):
            evidence = Evidence.from_dict(evidence)
        verdict = self.verifier_for(task).verify(task, evidence)
        if verdict is None:
            return self.unverified(f"缺少回测指标 {self.metric}，无法客观裁决")
        verdict.task_id = evidence.task if not verdict.task_id else verdict.task_id
        return verdict

    # -- 奖励信号 ----------------------------------------------------------
    def reward_signal(self, task: str, result: Any) -> float:
        """从回测结果 / 证据 / 裁决里提取夏普作为奖励信号（无法提取 → 0.0）。"""
        metrics = self._extract_metrics(result)
        try:
            return float(metrics.get(self.metric, 0.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _extract_metrics(result: Any) -> Dict[str, float]:
        if isinstance(result, Evidence):
            return dict(result.metrics)
        if isinstance(result, dict):
            if "metrics" in result and isinstance(result["metrics"], dict):
                return {k: float(v) for k, v in result["metrics"].items()}
            return {k: float(v) for k, v in result.items() if isinstance(v, (int, float))}
        return {}

    # -- 回测（可选，注入 backtest 时用）----------------------------------
    def run_backtest(self, task: str, data: Optional[Dict[str, Any]] = None) -> Evidence:
        """用注入的回测器跑 ``task``，把指标打包为 :class:`Evidence`。

        无回测器或异常 → 空 Evidence（后续 verify 会判 UNVERIFIED）。
        """
        if not callable(self._backtest):
            return Evidence(task=task)
        try:
            metrics = self._backtest(task, data or {})
        except Exception as exc:
            logger.warning("evolution.finance: 回测失败 → 空证据: %s", exc)
            return Evidence(task=task, logs=f"回测异常：{exc}")
        clean: Dict[str, float] = {}
        if isinstance(metrics, dict):
            for k, v in metrics.items():
                try:
                    clean[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        return Evidence(task=task, metrics=clean)


__all__ = ["FinanceDomain", "DataSource", "BacktestFn"]
