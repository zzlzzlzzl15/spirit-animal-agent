"""文档 / 专有平台集成测试 —— 飞书（SDK 门控）+ 元宝（上下文门控）。"""

from spirit.integrations.docs import FeishuDocIntegration, YuanbaoIntegration


# --------------------------------------------------------------------------
# 飞书
# --------------------------------------------------------------------------

def test_feishu_read_doc_requires_token():
    integ = FeishuDocIntegration(env={})
    res = integ.read_doc("")
    assert res.ok is False
    assert "doc_token" in res.error


def test_feishu_read_doc_fails_gracefully():
    """无论 SDK 是否安装，read_doc 都应优雅返回 ok=False（不抛）。"""
    integ = FeishuDocIntegration(env={"FEISHU_APP_ID": "x"})
    res = integ.read_doc("doc123")
    assert res.ok is False
    assert isinstance(res.error, str) and res.error


def test_feishu_sdk_gates_configured():
    """requires_sdk=lark_oapi：SDK 缺失时 is_configured 必为 False。"""
    import importlib.util
    integ = FeishuDocIntegration(env={"FEISHU_APP_ID": "x"})
    has_sdk = importlib.util.find_spec("lark_oapi") is not None
    if not has_sdk:
        assert integ.is_configured() is False
        assert any("sdk:" in m for m in integ.missing_requirements())


def test_feishu_describe():
    desc = FeishuDocIntegration(env={}).describe()
    assert desc["name"] == "feishu"
    assert desc["category"] == "docs"


# --------------------------------------------------------------------------
# 元宝
# --------------------------------------------------------------------------

class _FakeContext:
    def __init__(self):
        self.calls = []

    def get_group_info(self, group_code=None):
        self.calls.append(("get_group_info", group_code))
        return {"group": group_code, "members": 42}

    def boom(self):
        raise RuntimeError("ctx boom")


def test_yuanbao_not_configured_without_context():
    integ = YuanbaoIntegration(env={})
    assert integ.is_configured() is False
    assert integ.missing_requirements() == ["gateway_context"]
    res = integ.call("get_group_info", group_code="g1")
    assert res.ok is False


def test_yuanbao_configured_with_context():
    ctx = _FakeContext()
    integ = YuanbaoIntegration(env={}, context=ctx)
    assert integ.is_configured() is True
    assert integ.missing_requirements() == []


def test_yuanbao_call_dispatches_to_context():
    ctx = _FakeContext()
    integ = YuanbaoIntegration(context=ctx)
    res = integ.call("get_group_info", group_code="g1")
    assert res.ok is True
    assert res.data["members"] == 42
    assert ctx.calls == [("get_group_info", "g1")]


def test_yuanbao_unknown_action():
    integ = YuanbaoIntegration(context=_FakeContext())
    res = integ.call("nonexistent_action")
    assert res.ok is False
    assert "不支持的操作" in res.error


def test_yuanbao_context_exception_caught():
    integ = YuanbaoIntegration(context=_FakeContext())
    res = integ.call("boom")
    assert res.ok is False
    assert "ctx boom" in res.error
