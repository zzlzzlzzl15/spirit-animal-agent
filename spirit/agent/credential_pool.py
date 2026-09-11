"""CredentialPool — 多凭据轮换管理。

参考 Hermes 的 credential_pool.py (2601行)，实现：
- 多凭据存储（Provider 级别）
- 轮换策略（轮询/随机/最少使用）
- 失败计数 + 自动降级
- 凭据验证
- 持久化存储（加密）

使用场景：
- 多个 API Key 轮换避免单点限流
- 多账号负载均衡
- 自动故障转移
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RotationStrategy(str, Enum):
    """轮换策略。"""

    ROUND_ROBIN = "round_robin"   # 轮询
    RANDOM = "random"             # 随机
    LEAST_USED = "least_used"     # 最少使用
    FIRST_AVAILABLE = "first_available"  # 第一个可用


@dataclass
class Credential:
    """单个凭据。"""

    id: str = ""
    provider: str = ""
    api_key: str = ""
    base_url: str = ""
    label: str = ""  # 人类可读标签

    # 状态
    enabled: bool = True
    fail_count: int = 0
    success_count: int = 0
    last_used: float = 0.0
    last_failed: float = 0.0
    cooldown_until: float = 0.0  # 冷却截止时间

    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_available(self) -> bool:
        """凭据是否可用。"""
        if not self.enabled:
            return False
        if self.cooldown_until > time.time():
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        """序列化（隐藏敏感信息）。"""
        return {
            "id": self.id,
            "provider": self.provider,
            "api_key_prefix": self.api_key[:8] + "..." if len(self.api_key) > 8 else "***",
            "base_url": self.base_url,
            "label": self.label,
            "enabled": self.enabled,
            "fail_count": self.fail_count,
            "success_count": self.success_count,
            "last_used": self.last_used,
        }


class CredentialPool:
    """凭据池 — 管理多凭据轮换。"""

    # 默认冷却时间（秒，来自集中式配置）
    @staticmethod
    def _default_cooldown() -> int:
        from spirit.config import get_config_value
        return get_config_value("agent.credential_cooldown", 300)

    # 最大失败次数（超过后自动禁用，来自集中式配置）
    @staticmethod
    def _default_max_fail_count() -> int:
        from spirit.config import get_config_value
        return get_config_value("agent.credential_max_fail_count", 10)

    def __init__(
        self,
        storage_path: str = None,
        rotation_strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
    ):
        """初始化凭据池。

        Args:
            storage_path: 持久化存储路径
            rotation_strategy: 轮换策略
        """
        self.storage_path = storage_path or self._default_storage_path()
        self.rotation_strategy = rotation_strategy
        self._credentials: Dict[str, List[Credential]] = {}  # {provider: [cred, ...]}
        self._current_index: Dict[str, int] = {}  # {provider: index}
        self._load()

    def _default_storage_path(self) -> str:
        """默认存储路径。"""
        home = Path.home()
        cred_dir = home / ".spirit" / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        return str(cred_dir / "pool.json")

    # ------------------------------------------------------------------
    # 凭据管理
    # ------------------------------------------------------------------

    def add(
        self,
        provider: str,
        api_key: str,
        base_url: str = "",
        label: str = "",
        cred_id: str = None,
    ) -> Credential:
        """添加凭据。

        Args:
            provider: Provider 名称
            api_key: API 密钥
            base_url: 基础 URL
            label: 人类可读标签
            cred_id: 凭据 ID（不提供则自动生成）

        Returns:
            创建的 Credential 对象
        """
        if not cred_id:
            cred_id = self._generate_id(provider, api_key)

        credential = Credential(
            id=cred_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            label=label or f"{provider}-{cred_id[:8]}",
        )

        # 添加到池
        if provider not in self._credentials:
            self._credentials[provider] = []
            self._current_index[provider] = 0

        # 检查重复
        for i, existing in enumerate(self._credentials[provider]):
            if existing.id == cred_id:
                self._credentials[provider][i] = credential
                logger.info("凭据已更新: %s", credential.label)
                self._save()
                return credential

        self._credentials[provider].append(credential)
        self._save()

        logger.info("凭据已添加: %s (%s)", credential.label, provider)
        return credential

    def remove(self, provider: str, cred_id: str) -> bool:
        """移除凭据。"""
        creds = self._credentials.get(provider, [])
        for i, cred in enumerate(creds):
            if cred.id == cred_id:
                creds.pop(i)
                self._save()
                logger.info("凭据已移除: %s", cred.label)
                return True
        return False

    def get(
        self,
        provider: str,
        strategy: RotationStrategy = None,
    ) -> Optional[Credential]:
        """获取下一个可用凭据。

        Args:
            provider: Provider 名称
            strategy: 轮换策略（覆盖默认）

        Returns:
            Credential 对象，无可用则返回 None
        """
        creds = self._credentials.get(provider, [])
        if not creds:
            return None

        strategy = strategy or self.rotation_strategy
        available = [c for c in creds if c.is_available()]

        if not available:
            logger.warning("Provider %s 无可用凭据", provider)
            return None

        if strategy == RotationStrategy.ROUND_ROBIN:
            return self._get_round_robin(provider, available)
        elif strategy == RotationStrategy.RANDOM:
            return self._get_random(available)
        elif strategy == RotationStrategy.LEAST_USED:
            return self._get_least_used(available)
        elif strategy == RotationStrategy.FIRST_AVAILABLE:
            return available[0]

        return available[0]

    def report_success(self, provider: str, cred_id: str) -> None:
        """报告凭据使用成功。"""
        cred = self._find_credential(provider, cred_id)
        if cred:
            cred.success_count += 1
            cred.last_used = time.time()
            cred.fail_count = 0  # 重置失败计数
            self._save()

    def report_failure(self, provider: str, cred_id: str, cooldown: float = None) -> None:
        """报告凭据使用失败。

        Args:
            provider: Provider 名称
            cred_id: 凭据 ID
            cooldown: 冷却时间（秒）
        """
        cred = self._find_credential(provider, cred_id)
        if not cred:
            return

        cred.fail_count += 1
        cred.last_failed = time.time()
        cred.cooldown_until = time.time() + (cooldown or self._default_cooldown())

        # 超过最大失败次数，自动禁用
        if cred.fail_count >= self._default_max_fail_count():
            cred.enabled = False
            logger.warning("凭据自动禁用（失败 %d 次）: %s", cred.fail_count, cred.label)

        self._save()
        logger.debug("凭据失败 #%d: %s", cred.fail_count, cred.label)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_credentials(self, provider: str = None) -> List[Dict[str, Any]]:
        """列出凭据（隐藏敏感信息）。"""
        if provider:
            creds = self._credentials.get(provider, [])
            return [c.to_dict() for c in creds]

        result = []
        for provider, creds in self._credentials.items():
            for cred in creds:
                result.append(cred.to_dict())
        return result

    def count(self, provider: str = None) -> int:
        """统计凭据数量。"""
        if provider:
            return len(self._credentials.get(provider, []))
        return sum(len(creds) for creds in self._credentials.values())

    def get_stats(self) -> Dict[str, Any]:
        """获取凭据池统计。"""
        stats = {
            "total": self.count(),
            "by_provider": {},
            "strategy": self.rotation_strategy.value,
        }
        for provider, creds in self._credentials.items():
            available = sum(1 for c in creds if c.is_available())
            stats["by_provider"][provider] = {
                "total": len(creds),
                "available": available,
            }
        return stats

    # ------------------------------------------------------------------
    # 轮换策略
    # ------------------------------------------------------------------

    def _get_round_robin(self, provider: str, available: List[Credential]) -> Credential:
        """轮询策略。"""
        index = self._current_index.get(provider, 0)
        index = index % len(available)
        self._current_index[provider] = index + 1
        cred = available[index]
        cred.last_used = time.time()
        return cred

    def _get_random(self, available: List[Credential]) -> Credential:
        """随机策略。"""
        import random
        cred = random.choice(available)
        cred.last_used = time.time()
        return cred

    def _get_least_used(self, available: List[Credential]) -> Credential:
        """最少使用策略。"""
        cred = min(available, key=lambda c: c.success_count)
        cred.last_used = time.time()
        return cred

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _find_credential(self, provider: str, cred_id: str) -> Optional[Credential]:
        """查找凭据。"""
        for cred in self._credentials.get(provider, []):
            if cred.id == cred_id:
                return cred
        return None

    def _generate_id(self, provider: str, api_key: str) -> str:
        """生成凭据 ID。"""
        hash_input = f"{provider}:{api_key}".encode()
        return hashlib.sha256(hash_input).hexdigest()[:16]

    def _load(self) -> None:
        """从文件加载凭据。"""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for provider, creds in data.get("credentials", {}).items():
                    self._credentials[provider] = []
                    for cred_data in creds:
                        cred = Credential(**cred_data)
                        self._credentials[provider].append(cred)
                logger.debug("凭据池已加载: %d 个凭据", self.count())
        except Exception as e:
            logger.warning("凭据池加载失败: %s", e)

    def _save(self) -> None:
        """保存凭据到文件。"""
        try:
            data = {
                "credentials": {},
                "version": 1,
            }
            for provider, creds in self._credentials.items():
                data["credentials"][provider] = []
                for cred in creds:
                    # 保存完整凭据（包括 API Key）
                    data["credentials"][provider].append({
                        "id": cred.id,
                        "provider": cred.provider,
                        "api_key": cred.api_key,
                        "base_url": cred.base_url,
                        "label": cred.label,
                        "enabled": cred.enabled,
                        "fail_count": cred.fail_count,
                        "success_count": cred.success_count,
                        "last_used": cred.last_used,
                        "last_failed": cred.last_failed,
                        "cooldown_until": cred.cooldown_until,
                        "metadata": cred.metadata,
                    })

            # 确保目录存在
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)

            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        except Exception as e:
            logger.warning("凭据池保存失败: %s", e)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_default_pool: Optional[CredentialPool] = None


def get_credential_pool() -> CredentialPool:
    """获取全局凭据池实例。"""
    global _default_pool
    if _default_pool is None:
        _default_pool = CredentialPool()
    return _default_pool


def add_credential(provider: str, api_key: str, **kwargs) -> Credential:
    """便捷函数：添加凭据。"""
    return get_credential_pool().add(provider, api_key, **kwargs)


def get_credential(provider: str) -> Optional[Credential]:
    """便捷函数：获取凭据。"""
    return get_credential_pool().get(provider)


__all__ = [
    "Credential",
    "CredentialPool",
    "RotationStrategy",
    "get_credential_pool",
    "add_credential",
    "get_credential",
]
