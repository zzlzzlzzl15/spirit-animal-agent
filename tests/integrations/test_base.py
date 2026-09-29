"""集成框架基座测试 —— spec/registry/凭据解析/describe/health_check。"""

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    HttpResponse,
    IntegrationRegistry,
    IntegrationResult,
    IntegrationSpec,
)


def _spec(**kw):
    base = dict(name="demo", display_name="Demo", category=Category.MESSAGING)
    base.update(kw)
    return IntegrationSpec(**base)


# --------------------------------------------------------------------------
# IntegrationResult
# --------------------------------------------------------------------------

def test_result_success_failure():
    ok = IntegrationResult.success({"a": 1})
    assert ok.ok is True and ok.data == {"a": 1}
    bad = IntegrationResult.failure("boom")
    assert bad.ok is False and bad.error == "boom"
    assert bad.to_dict() == {"ok": False, "data": None, "error": "boom"}


def test_http_response_helpers():
    resp = HttpResponse(status=200, body=b'{"x": 1}')
    assert resp.ok is True
    assert resp.json() == {"x": 1}
    assert HttpResponse(status=404).ok is False
    assert HttpResponse(status=204).ok is True


# --------------------------------------------------------------------------
# 凭据解析
# --------------------------------------------------------------------------

def test_is_configured_required_env():
    integ = BaseIntegration(_spec(required_env=("TOKEN",)), env={"TOKEN": "x"})
    assert integ.is_configured() is True
    empty = BaseIntegration(_spec(required_env=("TOKEN",)), env={})
    assert empty.is_configured() is False


def test_is_configured_any_of_env():
    spec = _spec(any_of_env=("A", "B"))
    assert BaseIntegration(spec, env={"B": "1"}).is_configured() is True
    assert BaseIntegration(spec, env={}).is_configured() is False


def test_is_configured_all_required():
    spec = _spec(required_env=("A", "B"))
    assert BaseIntegration(spec, env={"A": "1"}).is_configured() is False
    assert BaseIntegration(spec, env={"A": "1", "B": "2"}).is_configured() is True


def test_missing_requirements_lists_keys():
    integ = BaseIntegration(_spec(required_env=("A", "B")), env={"A": "1"})
    assert integ.missing_requirements() == ["B"]


def test_missing_requirements_any_of():
    integ = BaseIntegration(_spec(any_of_env=("A", "B")), env={})
    assert integ.missing_requirements() == ["any_of:A|B"]


def test_env_reads_injected_map():
    integ = BaseIntegration(_spec(), env={"FOO": "  bar  "})
    assert integ.getenv("FOO") == "bar"  # 自动 strip
    assert integ.getenv("MISSING", "dflt") == "dflt"


def test_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("SPIRIT_TEST_INTEG_VAR", "hello")
    integ = BaseIntegration(_spec())
    assert integ.getenv("SPIRIT_TEST_INTEG_VAR") == "hello"


def test_requires_sdk_gates_configured():
    spec = _spec(requires_sdk="nonexistent_sdk_xyz")
    integ = BaseIntegration(spec, env={})
    assert integ.is_configured() is False
    assert "sdk:nonexistent_sdk_xyz" in integ.missing_requirements()


# --------------------------------------------------------------------------
# describe / health_check
# --------------------------------------------------------------------------

def test_describe_shape():
    integ = BaseIntegration(
        _spec(required_env=("T",), capabilities=("read",), emoji="🔌",
              description="d", docs_url="http://x"),
        env={"T": "1"},
    )
    desc = integ.describe()
    assert desc["name"] == "demo"
    assert desc["configured"] is True
    assert desc["capabilities"] == ["read"]
    assert desc["emoji"] == "🔌"
    assert desc["docs_url"] == "http://x"
    assert desc["missing"] == []


def test_health_check_configured():
    integ = BaseIntegration(_spec(required_env=("T",)), env={"T": "1"})
    assert integ.health_check().ok is True


def test_health_check_not_configured():
    integ = BaseIntegration(_spec(required_env=("T",)), env={})
    res = integ.health_check()
    assert res.ok is False
    assert "T" in res.error


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_registry_register_get_list():
    reg = IntegrationRegistry()
    a = BaseIntegration(_spec(name="a", category=Category.MESSAGING))
    b = BaseIntegration(_spec(name="b", category=Category.SEARCH))
    reg.register(a)
    reg.register(b)
    assert reg.get("a") is a
    assert reg.get("nope") is None
    assert reg.names() == ["a", "b"]
    assert [i.spec.name for i in reg.list(category=Category.SEARCH)] == ["b"]


def test_registry_categories_and_configured():
    reg = IntegrationRegistry()
    reg.register(BaseIntegration(_spec(name="a", category=Category.MESSAGING,
                                       required_env=("T",)), env={"T": "1"}))
    reg.register(BaseIntegration(_spec(name="b", category=Category.SEARCH,
                                       required_env=("MISSING",)), env={}))
    assert reg.categories() == [Category.MESSAGING, Category.SEARCH]
    assert [i.spec.name for i in reg.configured()] == ["a"]


def test_registry_clear():
    reg = IntegrationRegistry()
    reg.register(BaseIntegration(_spec(name="a")))
    reg.clear()
    assert reg.names() == []
