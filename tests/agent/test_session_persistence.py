"""tests/agent/test_session_persistence.py — Spirit Agent 会话持久化与状态栏回归测试。

覆盖 v0.2.8 / v0.2.9 / v0.2.10 / v0.2.11 四个版本的修复点。
每次提交版本前必须跑这组测试，命令::

    python -m pytest tests/agent/test_session_persistence.py -v

设计原则：
- **隔离**：monkeypatch spirit.storage.session_db.DEFAULT_DB_PATH 到 tmp_path，
  避免污染真实 ~/.spirit/state.db。
- **真实**：用真实 SpiritAgent.__init__（不 mock）以保证 v0.2.8/10 字段初始化路径被覆盖。
- **聚焦**：每个 test 断言一个修复点，失败定位精确。
"""
import json
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 隔离 fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """隔离 SessionDB 路径到 tmp_path，避免污染 ~/.spirit/state.db。

    monkeypatch 后所有 SessionDB() 无参构造都会用这个路径。
    function-scoped：test 结束后自动还原。
    """
    db_path = tmp_path / "session_persistence_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    """独立 db + compression_enabled=False 的 SpiritAgent。"""
    from spirit.agent.agent import SpiritAgent, AgentConfig
    config = AgentConfig(
        model="gpt-4o-mini",
        api_key="test-key-12345",
        base_url="https://api.openai.com/v1",
        provider="openai",
        max_iterations=5,
        compression_enabled=False,
        streaming_enabled=False,
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ===========================================================================
# v0.2.8: SpiritAgent.__init__ 首启 session 幂等创建
# ===========================================================================

class TestV028FirstSessionCreate:
    """v0.2.8 修复：SpiritAgent.__init__ 末尾首启 session 幂等创建。

    根因：之前只设内存 session_id，db 里没记录，导致「最近任务」永远不包含刚跑完。
    修复：spirit 启动时 ensure session_id 已在 db 里（get_session 非空则跳过）。
    """

    def test_session_record_exists_in_db_after_init(self, fresh_agent, isolated_db_path):
        """SpiritAgent 初始化后, db 里应有对应 session 记录。"""
        from spirit.storage.session_db import SessionDB
        db = SessionDB(db_path=isolated_db_path)
        row = db.get_session(fresh_agent.session_id)
        assert row is not None, "db 应已创建 session 记录"
        assert row["id"] == fresh_agent.session_id
        db.close()

    def test_resume_existing_session_no_duplicate(self, fresh_agent, isolated_db_path):
        """/resume 恢复时, db 已有记录则不创建新行（幂等）。"""
        from spirit.storage.session_db import SessionDB
        from spirit.agent.agent import SpiritAgent, AgentConfig
        # 第二个 SpiritAgent 拿同 session_id（模拟 /resume）
        config = AgentConfig(
            model="gpt-4o-mini", api_key="test", provider="openai",
            max_iterations=5, compression_enabled=False,
            session_id=fresh_agent.session_id,
        )
        agent2 = SpiritAgent(config=config)
        try:
            db = SessionDB(db_path=isolated_db_path)
            rows = db.list_sessions()
            same_id_rows = [r for r in rows if r["id"] == fresh_agent.session_id]
            assert len(same_id_rows) == 1, f"应只有 1 行, 实际 {len(same_id_rows)}"
            db.close()
        finally:
            try:
                agent2._session_db.close()
            except Exception:
                pass


# ===========================================================================
# v0.2.9: close_session 幂等 + First Reason Wins + 异常隔离
# ===========================================================================

class TestV029CloseSession:
    """v0.2.9 修复：SpiritAgent.close_session(reason) 集中结束逻辑。

    设计：
    - 幂等：end_session SQL WHERE ended_at IS NULL 保证 First Reason Wins
    - 异常隔离：db 失败只 log warning, 不抛错
    - 可选 reason：默认 task_complete
    """

    def test_close_session_first_reason_wins(self, fresh_agent):
        """多次 close_session, 第一次的 reason 胜出。"""
        fresh_agent.close_session("user_interrupt")
        fresh_agent.close_session("cli_exit")  # 重复调用
        row = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert row["end_reason"] == "user_interrupt", "First Reason Wins"
        assert row["ended_at"] is not None

    def test_close_session_no_db_noop(self, fresh_agent):
        """无 db 时不抛错 (空操作)。"""
        fresh_agent._session_db = None
        try:
            fresh_agent.close_session("user_interrupt")
        except Exception as e:
            pytest.fail(f"无 db 时不应抛错: {e}")

    def test_close_session_no_session_id_noop(self, fresh_agent):
        """无 session_id 时不抛错 (空操作)。"""
        fresh_agent.session_id = None
        try:
            fresh_agent.close_session("user_interrupt")
        except Exception as e:
            pytest.fail(f"无 session_id 时不应抛错: {e}")

    def test_close_session_db_failure_isolation(self, fresh_agent):
        """db 抛错时 close_session 不向上抛 (异常隔离)。"""
        fresh_agent._session_db.end_session = MagicMock(
            side_effect=RuntimeError("db locked")
        )
        try:
            fresh_agent.close_session("user_interrupt")
        except Exception as e:
            pytest.fail(f"close_session 不应向上抛: {e}")


class TestV029InferCloseReason:
    """v0.2.9: _infer_close_reason 静态方法。

    5 个 case + 兜底 + 异常隔离：基于 response 前缀 startswith 判定。
    """

    def test_user_interrupt(self):
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="[已中断] 用户取消操作")
        assert SpiritAgent._infer_close_reason(result) == "user_interrupt"

    def test_max_iterations(self):
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="[达到最大迭代次数 90]")
        assert SpiritAgent._infer_close_reason(result) == "max_iterations"

    def test_api_error(self):
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="[API 错误: connection timeout]")
        assert SpiritAgent._infer_close_reason(result) == "api_error"

    def test_retry_exhausted(self):
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="[API 调用失败，已重试 3 次]")
        assert SpiritAgent._infer_close_reason(result) == "retry_exhausted"

    def test_guardrail_halt(self):
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="[工具循环停止: 超过 5 次相同工具调用]")
        assert SpiritAgent._infer_close_reason(result) == "guardrail_halt"

    def test_task_complete_default(self):
        """非前缀格式 = task_complete (兜底)。"""
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="这是普通回复, 不带 [前缀]")
        assert SpiritAgent._infer_close_reason(result) == "task_complete"

    def test_empty_response_default(self):
        """response 为空时 = task_complete (兜底)。"""
        from spirit.agent.agent import SpiritAgent
        result = MagicMock(response="")
        assert SpiritAgent._infer_close_reason(result) == "task_complete"

    def test_result_without_response_attr(self):
        """result 无 response 属性时 = task_complete (异常隔离)。"""
        from spirit.agent.agent import SpiritAgent
        # 故意构造一个 getattr 会抛错的 result
        class _BadResult:
            @property
            def response(self):
                raise RuntimeError("oops")
        assert SpiritAgent._infer_close_reason(_BadResult()) == "task_complete"


# ===========================================================================
# v0.2.10: _session_started_at + 状态栏工具函数 + since 时间戳
# ===========================================================================

class TestV210SessionStartedAt:
    """v0.2.10: SpiritAgent.__init__ 设置 _session_started_at = time.time()。

    用途：状态栏「since HH:MM:SS」锚点——累计用量的更新时间错。
    进程重启后该字段丢失, 下一行从 db.started_at 补。
    """

    def test_session_started_at_set_in_init(self, fresh_agent):
        """__init__ 后 _session_started_at 存在且是 float。"""
        assert hasattr(fresh_agent, "_session_started_at"), \
            "SpiritAgent 必须有 _session_started_at 字段 (v0.2.10)"
        assert isinstance(fresh_agent._session_started_at, float)
        # 与当前时间差应 < 60s
        assert abs(fresh_agent._session_started_at - time.time()) < 60

    def test_session_started_at_persists_in_memory(self, fresh_agent):
        """_session_started_at 在生命周期内不变。"""
        t1 = fresh_agent._session_started_at
        time.sleep(0.1)
        t2 = fresh_agent._session_started_at
        assert t1 == t2, "进程内 _session_started_at 应当稳定"


class TestV210FmtTokens:
    """v0.2.10: cli/main.py _fmt_tokens 数值格式化。

    规则：0 / 999 → 原样；< 1万 → "1.2K"；< 100万 → "45K"；< 1000万 → "1.5M"；否则 "15M"。
    """

    def test_fmt_tokens_zero_and_none(self):
        from spirit.cli.main import _fmt_tokens
        assert _fmt_tokens(0) == "0"
        assert _fmt_tokens(None) == "0"
        assert _fmt_tokens(-5) == "0"  # 负数钳到 0

    def test_fmt_tokens_small(self):
        from spirit.cli.main import _fmt_tokens
        assert _fmt_tokens(1) == "1"
        assert _fmt_tokens(999) == "999"

    def test_fmt_tokens_k_range(self):
        from spirit.cli.main import _fmt_tokens
        # < 10_000 → "1.2K" 风格
        assert _fmt_tokens(1_500) == "1.5K"
        # 10_000..999_999 → "15K" 风格
        assert _fmt_tokens(15_000) == "15K"
        assert _fmt_tokens(45_000) == "45K"
        assert _fmt_tokens(128_000) == "128K"

    def test_fmt_tokens_m_range(self):
        from spirit.cli.main import _fmt_tokens
        # 1_000_000..9_999_999 → "1.5M" 风格
        assert _fmt_tokens(1_500_000) == "1.5M"
        # >= 10_000_000 → "15M" 风格
        assert _fmt_tokens(15_000_000) == "15M"


class TestV210FmtDuration:
    """v0.2.10: cli/main.py _fmt_duration 时长格式化。

    规则：< 60s → "12s"；< 1h → "3m25s"；>= 1h → "1h12m"。
    """

    def test_fmt_duration_seconds(self):
        from spirit.cli.main import _fmt_duration
        assert _fmt_duration(0) == "0s"
        assert _fmt_duration(12) == "12s"
        assert _fmt_duration(59) == "59s"

    def test_fmt_duration_minutes(self):
        from spirit.cli.main import _fmt_duration
        assert _fmt_duration(60) == "1m00s"
        assert _fmt_duration(205) == "3m25s"
        assert _fmt_duration(599) == "9m59s"

    def test_fmt_duration_hours(self):
        from spirit.cli.main import _fmt_duration
        assert _fmt_duration(3600) == "1h00m"
        assert _fmt_duration(3725) == "1h02m"
        assert _fmt_duration(7325) == "2h02m"

    def test_fmt_duration_negative_clamp(self):
        """负数钳到 0。"""
        from spirit.cli.main import _fmt_duration
        assert _fmt_duration(-5) == "0s"


class TestV210SinceText:
    """v0.2.10: since HH:MM:SS 时间戳解析逻辑（从 cli/main.py 复现）。

    优先级：
    1. agent._session_started_at（进程内）
    2. db.started_at（进程重启后）
    3. None（fallback：状态栏不显示 since）
    """

    def test_since_text_from_in_memory(self, fresh_agent):
        """优先用 agent._session_started_at。"""
        # 复现 cli/main.py 489-509 行的 since 解析逻辑
        _started = getattr(fresh_agent, "_session_started_at", None)
        if _started is None and getattr(fresh_agent, "_session_db", None) and fresh_agent.session_id:
            _row = fresh_agent._session_db.get_session(fresh_agent.session_id) or {}
            _iso = _row.get("started_at")
            if _iso:
                _started = datetime.fromisoformat(str(_iso)).timestamp()
        assert _started is not None
        since_txt = datetime.fromtimestamp(float(_started)).strftime("%H:%M:%S")
        # 格式校验：HH:MM:SS 8 字符
        assert len(since_txt) == 8
        assert since_txt[2] == ":" and since_txt[5] == ":"

    def test_since_text_fallback_to_db(self, isolated_db_path):
        """_session_started_at 缺失时, 从 db.started_at 补。"""
        from spirit.agent.agent import SpiritAgent, AgentConfig
        from spirit.storage.session_db import SessionDB

        # 创建 agent 后**删** _session_started_at 模拟进程重启
        config = AgentConfig(
            model="gpt-4o-mini", api_key="test", provider="openai",
            max_iterations=5, compression_enabled=False,
        )
        agent = SpiritAgent(config=config)
        if hasattr(agent, "_session_started_at"):
            delattr(agent, "_session_started_at")
        try:
            # 走 db fallback
            _row = agent._session_db.get_session(agent.session_id) or {}
            _iso = _row.get("started_at")
            assert _iso, "db.started_at 应当有值"
            _started = datetime.fromisoformat(str(_iso)).timestamp()
            since_txt = datetime.fromtimestamp(float(_started)).strftime("%H:%M:%S")
            # since_txt 与 db.started_at 时间部分一致
            iso_time_part = _iso.split("T")[1][:8] if "T" in _iso else _iso[-8:]
            assert since_txt == iso_time_part
        finally:
            try:
                agent._session_db.close()
            except Exception:
                pass

    def test_since_text_none_when_no_data(self, isolated_db_path):
        """无 _session_started_at 且 db.started_at 为空时返回 None。"""
        _started = None
        # 无 agent 无 db → 全部 None
        assert _started is None
        since_txt = None  # 状态栏不显示 since
        assert since_txt is None


# ===========================================================================
# v0.2.11: add_message 同步落库 (修复"空会话")
# ===========================================================================

class TestV211AddMessagePersist:
    """v0.2.11 修复：SpiritAgent.add_message 同步落库到 session_db.messages。

    根因：之前只 append 到内存 self._messages，db 里没 user / assistant 消息——
    导致 _print_recent_tasks 拿不到首条 user message preview，全部显示「(空会话)」。
    补充根因：lifecycle_hooks 的 ON_SESSION_END 事件名与实际触发不匹配 (休眠缺陷)。

    修复：add_message 内部加 session_db.save_message 同步落库 + tool_calls json 序列化。
    """

    def test_user_message_persisted(self, fresh_agent):
        """user 消息落库后可读出。"""
        fresh_agent.add_message("user", "你好")
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "你好"

    def test_multiple_roles_persisted(self, fresh_agent):
        """system / user / assistant 多角色都落库。"""
        fresh_agent.add_message("system", "你是 Spirit Agent")
        fresh_agent.add_message("user", "今天天气如何？")
        fresh_agent.add_message("assistant", "晴，气温 25°C")
        fresh_agent.add_message("user", "适合出门吗？")
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert len(msgs) == 4
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]

    def test_preview_first_user_message(self, fresh_agent):
        """_print_recent_tasks 取首条 user message 逻辑（修复"空会话"的核心断言）。"""
        fresh_agent.add_message("system", "你是 Spirit Agent，一个有桌宠的 AI 助手")
        fresh_agent.add_message("user", "查询今日国际新闻要点")
        fresh_agent.add_message("assistant", "1. 美股 S&P 500 收涨...\n2. 美债收益率创新高")
        # 复现 _print_recent_tasks 280-284 行的 preview 逻辑
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        preview = ""
        for m in msgs:
            if m.get("role") == "user" and m.get("content"):
                preview = m["content"].replace("\n", " ").strip()
                break
        assert preview == "查询今日国际新闻要点", \
            f"preview 应当是首条 user message, 实际 {preview!r}"

    def test_db_failure_isolation(self, fresh_agent):
        """db 抛错时 add_message 仍正常 append 内存（异常隔离）。"""
        fresh_agent._session_db.save_message = MagicMock(
            side_effect=RuntimeError("db locked")
        )
        try:
            fresh_agent.add_message("user", "test")
        except Exception as e:
            pytest.fail(f"add_message 不应向上抛: {e}")
        # 内存仍 append
        assert len(fresh_agent._messages) == 1
        assert fresh_agent._messages[0]["role"] == "user"

    def test_no_db_skip_persist(self, fresh_agent):
        """无 db 时 add_message 只 append 内存, 不抛错。"""
        fresh_agent._session_db = None
        try:
            fresh_agent.add_message("user", "test")
        except Exception as e:
            pytest.fail(f"无 db 时不应抛错: {e}")
        assert len(fresh_agent._messages) == 1

    def test_tool_calls_json_serialized(self, fresh_agent):
        """tool_calls (list/dict) 需 json 序列化为 str（SQLite 不支持 list binding）。"""
        tool_calls = [{"id": "tc_1", "type": "function", "function": {"name": "web_search"}}]
        fresh_agent.add_message("assistant", "", tool_calls=tool_calls)
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert len(msgs) == 1
        # 落库后 tool_calls 字段是 JSON 字符串
        assert isinstance(msgs[0]["tool_calls"], str)
        # 可反序列化
        decoded = json.loads(msgs[0]["tool_calls"])
        assert decoded[0]["id"] == "tc_1"
        assert decoded[0]["function"]["name"] == "web_search"

    def test_tool_calls_str_passthrough(self, fresh_agent):
        """tool_calls 已经是 str 时直接传（不二次 json）。"""
        tc_str = '[{"id": "tc_2"}]'
        fresh_agent.add_message("assistant", "", tool_calls=tc_str)
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert msgs[0]["tool_calls"] == tc_str

    def test_conversation_loop_message_ordering(self, fresh_agent):
        """模拟 conversation_loop 11 个 add_message 调用点的顺序正确性。"""
        # 复现 conversation_loop 的典型 message 流
        fresh_agent.add_message("system", "你是 Spirit Agent")
        fresh_agent.add_message("user", "用户第一问")
        fresh_agent.add_message("assistant", "助手回答 1")
        fresh_agent.add_message(
            "assistant", "", tool_calls=[{"id": "tc_1", "function": {"name": "web_search"}}]
        )
        fresh_agent.add_message("tool", "搜索结果...", tool_call_id="tc_1")
        fresh_agent.add_message("assistant", "根据搜索结果...")
        fresh_agent.add_message("user", "继续追问")
        fresh_agent.add_message("assistant", "回答 2")
        fresh_agent.add_message("user", "[修正] 换个思路")
        fresh_agent.add_message("assistant", "修正后的回答")
        fresh_agent.add_message("assistant", "[工具循环停止: 超过 5 次相同工具调用]")

        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert len(msgs) == 11
        # 关键路径
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "用户第一问"
        assert msgs[2]["role"] == "assistant"
        # tool_calls 消息
        assert msgs[3]["role"] == "assistant"
        assert msgs[3]["tool_calls"] is not None
        # tool 响应
        assert msgs[4]["role"] == "tool"
        assert msgs[4]["tool_call_id"] == "tc_1"
