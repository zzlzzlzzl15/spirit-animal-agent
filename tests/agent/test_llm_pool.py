"""多 provider LLM 故障转移池测试（spirit/llm_pool.py）。

语义：**"哪个有 token 用哪个"** —— 按优先级探测 provider 健康度，选第一个健康的。
本组测试全离线（probe 打桩），锁定：

1. ``load_providers``：failover 列表解析（inline 优先 / profile+env 补齐 / 缺字段跳过 /
   无列表时回退 llm 顶层单 provider）；
2. ``failover_enabled``：开关默认 False（不改变单 provider 行为）；
3. ``probe``：HTTP 200 + 去 think 块后正文非空才算健康；空正文/非 200/异常均不健康；
4. ``pick_provider``：按序选中首个健康者 + TTL 缓存 + 全不健康回退首个（不缓存）+ 无配置返回 None；
5. ``any_healthy``：任一健康即 True（批量闸门语义），无配置 True（不拦截）；
6. ``invalidate_cache``：清缓存后强制重探。
"""

from __future__ import annotations

import json

import pytest

import spirit.llm_pool as pool


# =========================================================================
# fixtures / 辅助
# =========================================================================

@pytest.fixture
def spirit_home(tmp_path, monkeypatch):
    """把 SPIRIT_HOME 隔离到 tmp_path（llm_pool._config_path 按调用读环境变量）。"""
    monkeypatch.setenv("SPIRIT_HOME", str(tmp_path))
    return tmp_path


def write_config(home, providers=None, enabled=True, top_level_llm=None):
    """在隔离的 SPIRIT_HOME 写 config.yaml。

    providers 为 None 时不写 failover.providers 节；enabled=False 时写 enabled: false。
    top_level_llm 合并进 llm 顶层（用于回退单 provider 场景）。
    """
    import yaml

    llm = dict(top_level_llm or {})
    llm["failover"] = {"enabled": enabled}
    if providers is not None:
        llm["failover"]["providers"] = providers
    (home / "config.yaml").write_text(
        yaml.safe_dump({"llm": llm}, allow_unicode=True), encoding="utf-8",
    )


def _inline(name="qwen", provider="openai", model="qwen3-max",
            api_key="sk-inline", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"):
    return {
        "name": name, "provider": provider, "model": model,
        "api_key": api_key, "base_url": base_url,
    }


class _FakeResp:
    """urlopen 的最小替身：带 status + read()。"""

    def __init__(self, payload, status=200):
        self._payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_probe(monkeypatch, healthy_names):
    """把 pool.probe 打桩为"名字在 healthy_names 里即健康"，并记录探测顺序。"""
    seen = []

    def fake_probe(p, timeout=30):
        seen.append(p.get("name"))
        return p.get("name") in healthy_names

    monkeypatch.setattr(pool, "probe", fake_probe)
    return seen


# =========================================================================
# 1. load_providers
# =========================================================================

class TestLoadProviders:

    def test_inline_values_win(self, spirit_home):
        """条目自带 api_key/base_url 时直接用，不去查注册表/环境变量。"""
        write_config(spirit_home, providers=[_inline()])

        got = pool.load_providers()

        assert len(got) == 1
        assert got[0]["api_key"] == "sk-inline"
        assert got[0]["base_url"].endswith("/compatible-mode/v1")
        assert got[0]["model"] == "qwen3-max"

    def test_missing_base_url_and_key_is_skipped(self, spirit_home, monkeypatch):
        """inline 与 profile 都凑不齐 base_url+api_key → 无法探测 → 跳过该条目。"""
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("MINIMAX_CN_API_KEY", raising=False)
        monkeypatch.setattr(pool, "_resolve_profile", lambda name: None)
        write_config(spirit_home, providers=[{"name": "orphan", "provider": "orphan", "model": "m"}])

        assert pool.load_providers() == []

    def test_profile_fills_base_url_and_env_key(self, spirit_home, monkeypatch):
        """inline 缺 base_url/api_key 时从 ProviderProfile + 环境变量补齐。"""
        class _Prof:
            base_url = "https://api.example.com/v1"
            env_vars = ("EXAMPLE_API_KEY",)

        monkeypatch.setenv("EXAMPLE_API_KEY", "sk-from-env")
        monkeypatch.setattr(pool, "_resolve_profile", lambda name: _Prof() if name == "example" else None)
        write_config(spirit_home, providers=[{"name": "example", "provider": "example", "model": "m1"}])

        got = pool.load_providers()

        assert len(got) == 1
        assert got[0]["base_url"] == "https://api.example.com/v1"
        assert got[0]["api_key"] == "sk-from-env"

    def test_falls_back_to_top_level_llm(self, spirit_home):
        """无 failover 列表 → 回退 llm 顶层单 provider（保持原有行为）。"""
        write_config(
            spirit_home, providers=None,
            top_level_llm={"provider": "openai", "model": "gpt-4o",
                           "api_key": "sk-top", "base_url": "https://api.openai.com/v1"},
        )

        got = pool.load_providers()

        assert len(got) == 1
        assert got[0]["model"] == "gpt-4o"
        assert got[0]["api_key"] == "sk-top"

    def test_no_config_file_returns_empty(self, spirit_home):
        """配置缺失 → 空列表（绝不抛）。"""
        assert pool.load_providers() == []

    def test_malformed_entries_ignored(self, spirit_home):
        """非 dict 条目被忽略，合法条目照常返回。"""
        write_config(spirit_home, providers=["not-a-dict", 42, _inline(name="ok")])

        got = pool.load_providers()

        assert [p["name"] for p in got] == ["ok"]

    def test_priority_order_preserved(self, spirit_home):
        """列表顺序即优先级，必须原样保留。"""
        write_config(spirit_home, providers=[
            _inline(name="a", base_url="https://a.example/v1"),
            _inline(name="b", base_url="https://b.example/v1"),
            _inline(name="c", base_url="https://c.example/v1"),
        ])

        assert [p["name"] for p in pool.load_providers()] == ["a", "b", "c"]


# =========================================================================
# 2. failover_enabled
# =========================================================================

class TestFailoverEnabled:

    def test_default_false(self, spirit_home):
        """未配置 failover 节 → False（单 provider 行为零改变）。"""
        assert pool.failover_enabled() is False

    def test_explicit_true(self, spirit_home):
        write_config(spirit_home, providers=[_inline()], enabled=True)
        assert pool.failover_enabled() is True

    def test_explicit_false(self, spirit_home):
        write_config(spirit_home, providers=[_inline()], enabled=False)
        assert pool.failover_enabled() is False

    def test_broken_yaml_returns_false(self, spirit_home):
        """YAML 损坏 → False（绝不抛，回退单 provider）。"""
        (spirit_home / "config.yaml").write_text("llm:\n  failover: [\nbroken", encoding="utf-8")
        assert pool.failover_enabled() is False


# =========================================================================
# 3. probe（真实小请求探活，urlopen 打桩）
# =========================================================================

def _stub_urlopen(monkeypatch, payload, status=200, raise_exc=None):
    """打桩 urllib.request.urlopen（probe 内部按名字导入，故 patch 模块属性）。"""
    import urllib.request

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(payload, status=status)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


class TestProbe:

    def test_healthy_when_content_non_empty(self, monkeypatch):
        _stub_urlopen(monkeypatch, json.dumps(
            {"choices": [{"message": {"content": "正常"}}]}))

        assert pool.probe(_inline()) is True

    def test_think_block_stripped_before_check(self, monkeypatch):
        """去 think 块后正文为空 → 不健康（避免"只有推理没结论"被误判为可用）。"""
        _stub_urlopen(monkeypatch, json.dumps(
            {"choices": [{"message": {"content": "<think>x</think>   "}}]}))

        assert pool.probe(_inline()) is False

    def test_unhealthy_on_empty_content(self, monkeypatch):
        _stub_urlopen(monkeypatch, json.dumps({"choices": [{"message": {"content": ""}}]}))

        assert pool.probe(_inline()) is False

    def test_unhealthy_on_http_error(self, monkeypatch):
        """余额不足等 4xx/5xx → status != 200 → 不健康。"""
        _stub_urlopen(monkeypatch, json.dumps({"error": "insufficient_quota"}), status=402)

        assert pool.probe(_inline()) is False

    def test_unhealthy_on_connection_error(self, monkeypatch):
        """断供表现为连接错误 → 不健康（不抛）。"""
        _stub_urlopen(monkeypatch, "", raise_exc=OSError("connection refused"))

        assert pool.probe(_inline()) is False

    def test_request_carries_bearer_auth_and_max_tokens(self, monkeypatch):
        """探测请求必须带 Bearer key + 足够 max_tokens（思考模型 think 块吃预算）。"""
        calls = _stub_urlopen(monkeypatch, json.dumps(
            {"choices": [{"message": {"content": "正常"}}]}))

        pool.probe(_inline(api_key="sk-secret"))

        req = calls[0]
        assert req.get_header("Authorization") == "Bearer sk-secret"
        body = json.loads(req.data.decode("utf-8"))
        assert body["max_tokens"] >= 2000
        assert req.full_url.endswith("/chat/completions")


# =========================================================================
# 4. pick_provider
# =========================================================================

@pytest.fixture(autouse=True)
def _clear_cache():
    """每个测试前后清探测缓存，避免跨测试串味。"""
    pool.invalidate_cache()
    yield
    pool.invalidate_cache()


class TestPickProvider:

    def test_picks_first_healthy_in_priority_order(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[
            _inline(name="dead", base_url="https://dead.example/v1"),
            _inline(name="alive", base_url="https://alive.example/v1"),
            _inline(name="also-alive", base_url="https://also.example/v1"),
        ])
        seen = stub_probe(monkeypatch, {"alive", "also-alive"})

        picked = pool.pick_provider()

        assert picked["name"] == "alive"
        assert seen == ["dead", "alive"]  # 命中即止，不探测后面的

    def test_result_cached_within_ttl(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[_inline(name="a"), _inline(name="b", base_url="https://b/v1")])
        seen = stub_probe(monkeypatch, {"a"})

        first = pool.pick_provider()
        second = pool.pick_provider()

        assert first is second
        assert seen == ["a"]  # 第二次命中缓存，零探测（热路径不重复发请求）

    def test_cache_miss_after_ttl(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[_inline(name="a")])
        seen = stub_probe(monkeypatch, {"a"})

        pool.pick_provider(ttl=0.0)
        pool.pick_provider(ttl=0.0)

        assert seen == ["a", "a"]  # ttl=0 → 每次重探（自适应 token 变化）

    def test_force_bypasses_cache(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[_inline(name="a")])
        seen = stub_probe(monkeypatch, {"a"})

        pool.pick_provider()
        pool.pick_provider(force=True)

        assert len(seen) == 2

    def test_all_unhealthy_falls_back_to_first_without_caching(self, spirit_home, monkeypatch):
        """全断供 → 返回列表首个（让上层照常报错），且不写缓存（下次立即重探）。"""
        write_config(spirit_home, providers=[
            _inline(name="a"), _inline(name="b", base_url="https://b/v1"),
        ])
        seen = stub_probe(monkeypatch, set())

        picked = pool.pick_provider()

        assert picked["name"] == "a"
        assert seen == ["a", "b"]
        # 未缓存：下一次仍会重新探测两家
        pool.pick_provider()
        assert seen == ["a", "b", "a", "b"]

    def test_no_config_returns_none(self, spirit_home):
        """无配置 → None，调用方回退自己的默认逻辑。"""
        assert pool.pick_provider() is None


# =========================================================================
# 5. any_healthy（批量闸门）
# =========================================================================

class TestAnyHealthy:

    def test_true_when_any_provider_healthy(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[
            _inline(name="dead", base_url="https://dead/v1"),
            _inline(name="alive", base_url="https://alive/v1"),
        ])
        stub_probe(monkeypatch, {"alive"})

        assert pool.any_healthy() is True

    def test_false_when_all_providers_down(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[
            _inline(name="a"), _inline(name="b", base_url="https://b/v1"),
        ])
        stub_probe(monkeypatch, set())

        assert pool.any_healthy() is False

    def test_true_when_unconfigured(self, spirit_home):
        """无配置 → True（不拦截，交给 runner 自己报错）。"""
        assert pool.any_healthy() is True


# =========================================================================
# 6. invalidate_cache
# =========================================================================

class TestInvalidateCache:

    def test_cache_cleared_forces_reprobe(self, spirit_home, monkeypatch):
        write_config(spirit_home, providers=[_inline(name="a")])
        seen = stub_probe(monkeypatch, {"a"})

        pool.pick_provider()
        pool.invalidate_cache()
        pool.pick_provider()

        assert seen == ["a", "a"]

    def test_switch_provider_after_invalidation(self, spirit_home, monkeypatch):
        """token 变化场景：a 从健康转不健康，invalidate 后应改选 b。"""
        write_config(spirit_home, providers=[
            _inline(name="a"), _inline(name="b", base_url="https://b/v1"),
        ])
        healthy = {"a"}
        stub_probe(monkeypatch, healthy)

        assert pool.pick_provider()["name"] == "a"

        healthy.clear()
        healthy.add("b")
        pool.invalidate_cache()

        assert pool.pick_provider()["name"] == "b"
