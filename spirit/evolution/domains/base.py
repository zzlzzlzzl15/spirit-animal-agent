"""领域无关接口 —— Spirit Agent Phase 6.F（docs/13 §F1 / §九.5）。

自进化闭环要接到具体领域，需领域提供四种能力（docs/13 §F1）：

- :meth:`Domain.collect_info` —— 收集领域信息（行情/新闻/环境状态……）；
- :meth:`Domain.propose_tasks` —— 基于信息**自派任务**（不靠人工派活）；
- :meth:`Domain.verify` —— 领域**客观验证器**（金融用回测、代码用测试）；
- :meth:`Domain.reward_signal` —— 可客观计算的**奖励信号**。

> **接入新领域前先确认其 ``reward_signal`` 可客观计算**（docs/13 §九.5）：没有可靠外部
> 验证器的领域，自进化易空转。:class:`Domain` 提供 fail-soft 默认实现（返回空/UNVERIFIED），
> 子类按需覆盖；所有方法都不得抛异常打断闭环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from spirit.evolution.protocol import TargetVerdict, Verdict
from spirit.evolution.verifier import Evidence

logger = logging.getLogger(__name__)


@dataclass
class DomainInfo:
    """领域收集到的信息（结构化，供 ``propose_tasks`` 消费）。"""

    domain: str = ""
    data: Dict[str, Any] = field(default_factory=dict)   # 原始信息（行情/新闻等）
    summary: str = ""                                     # 人类可读摘要
    collected_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "data": dict(self.data),
            "summary": self.summary,
            "collected_at": self.collected_at,
        }


class Domain:
    """领域适配器基类（fail-soft 默认实现，子类覆盖）。"""

    name: str = "generic"

    def collect_info(self) -> DomainInfo:
        """收集领域信息。默认返回空信息（子类接入真实数据源）。"""
        return DomainInfo(domain=self.name)

    def propose_tasks(self, info: Optional[DomainInfo] = None, *, limit: int = 3) -> List[str]:
        """基于 ``info`` 自派一批任务描述。默认返回空列表。"""
        return []

    def verify(self, task: str, evidence: Evidence) -> Optional[Verdict]:
        """领域客观验证：返回 :class:`Verdict` 表示已裁决，``None`` 表示交上层回退。

        默认无法客观裁决（``None``）。子类应基于 ``reward_signal`` 给出接地裁决。
        """
        return None

    def reward_signal(self, task: str, result: Any) -> float:
        """可客观计算的奖励信号（默认 0.0）。"""
        return 0.0

    def verifier_for(self, task: str) -> Optional[Any]:
        """可选：返回适配本领域的 :class:`~spirit.evolution.verifier.DomainVerifier`。"""
        return None

    def unverified(self, reason: str, *, task_id: str = "") -> Verdict:
        """便捷构造一个 UNVERIFIED 裁决（基础设施/数据缺失时用，绝不当 PASS/FAIL）。"""
        return Verdict(
            verdict=TargetVerdict.UNVERIFIED, reason=reason or "领域无法客观裁决", task_id=task_id
        )


__all__ = ["Domain", "DomainInfo"]
