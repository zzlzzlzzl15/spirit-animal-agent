"""tests/agent/test_credential_pool.py — 凭据池测试。"""

import pytest
import time
from spirit.agent.credential_pool import CredentialPool, Credential, RotationStrategy


class TestCredential:
    """Credential 数据模型测试。"""

    def test_create_credential(self):
        cred = Credential(id="test-id", provider="openai", api_key="sk-test-123")
        assert cred.id == "test-id"
        assert cred.provider == "openai"
        assert cred.enabled is True

    def test_is_available(self):
        cred = Credential(provider="openai", api_key="sk-test")
        assert cred.is_available() is True
        cred.enabled = False
        assert cred.is_available() is False

    def test_cooldown(self):
        cred = Credential(provider="openai", api_key="sk-test")
        cred.cooldown_until = time.time() + 300
        assert cred.is_available() is False

    def test_to_dict_hides_key(self):
        cred = Credential(provider="openai", api_key="sk-test-1234567890")
        data = cred.to_dict()
        assert "sk-test-" in data["api_key_prefix"]
        assert "1234567890" not in data["api_key_prefix"]


class TestCredentialPool:
    """CredentialPool 凭据池测试。"""

    def test_add_credential(self, credential_pool):
        cred = credential_pool.add("openai", "sk-test-123", label="main")
        assert cred.provider == "openai"
        assert credential_pool.count() == 1

    def test_get_credential(self, credential_pool):
        credential_pool.add("openai", "sk-test-1")
        cred = credential_pool.get("openai")
        assert cred is not None
        assert cred.api_key == "sk-test-1"

    def test_get_no_credentials(self, credential_pool):
        assert credential_pool.get("nonexistent") is None

    def test_round_robin_rotation(self, credential_pool):
        credential_pool.add("openai", "sk-key-1")
        credential_pool.add("openai", "sk-key-2")
        credential_pool.add("openai", "sk-key-3")

        keys = set()
        for _ in range(6):
            cred = credential_pool.get("openai", RotationStrategy.ROUND_ROBIN)
            keys.add(cred.api_key)
        assert len(keys) == 3

    def test_report_success(self, credential_pool):
        cred = credential_pool.add("openai", "sk-test")
        credential_pool.report_success("openai", cred.id)
        updated = credential_pool.list_credentials("openai")[0]
        assert updated["success_count"] == 1

    def test_report_failure(self, credential_pool):
        cred = credential_pool.add("openai", "sk-test")
        credential_pool.report_failure("openai", cred.id, cooldown=60)
        # 应在冷却中
        assert credential_pool.get("openai") is None  # 唯一的凭据被冷却

    def test_auto_disable_after_max_failures(self, credential_pool):
        cred = credential_pool.add("openai", "sk-test")
        for _ in range(credential_pool._default_max_fail_count()):
            credential_pool.report_failure("openai", cred.id, cooldown=0)
        # 应被自动禁用
        assert credential_pool.get("openai") is None

    def test_remove_credential(self, credential_pool):
        cred = credential_pool.add("openai", "sk-test")
        assert credential_pool.remove("openai", cred.id) is True
        assert credential_pool.count() == 0

    def test_list_credentials(self, credential_pool):
        credential_pool.add("openai", "sk-test-1", label="key1")
        credential_pool.add("anthropic", "sk-ant-1", label="key2")
        all_creds = credential_pool.list_credentials()
        assert len(all_creds) == 2

    def test_get_stats(self, credential_pool):
        credential_pool.add("openai", "sk-1")
        credential_pool.add("openai", "sk-2")
        stats = credential_pool.get_stats()
        assert stats["total"] == 2
        assert stats["by_provider"]["openai"]["total"] == 2

    def test_persistence(self, tmp_path):
        path = str(tmp_path / "creds.json")
        pool1 = CredentialPool(storage_path=path)
        pool1.add("openai", "sk-persist")
        pool1._save()

        pool2 = CredentialPool(storage_path=path)
        assert pool2.count() == 1
        cred = pool2.get("openai")
        assert cred.api_key == "sk-persist"
