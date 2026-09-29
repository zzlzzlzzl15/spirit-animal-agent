"""领域适配器 —— Spirit Agent Phase 6.F（docs/13 §Phase F）。

领域无关接口（:mod:`spirit.evolution.domains.base`）+ 金融量化示例
（:mod:`spirit.evolution.domains.finance`）。自进化闭环借此接到具体领域：
收集信息 → 自派任务 → 客观验证 → 奖励信号。
"""

from spirit.evolution.domains.base import Domain, DomainInfo
from spirit.evolution.domains.finance import FinanceDomain

__all__ = ["Domain", "DomainInfo", "FinanceDomain"]
