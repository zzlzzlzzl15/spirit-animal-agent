"""测试 chat_stream 的延迟回调机制。"""

import sys
sys.path.insert(0, '.')

from spirit.agent.agent import SpiritAgent, AgentConfig

# 创建 Agent（使用 Mock 配置）
config = AgentConfig(
    model="test-model",
    api_key="test-key",
    base_url="http://localhost:9877",
)

agent = SpiritAgent(config)

# 模拟流式回调
received_deltas = []

def mock_delta_callback(text):
    """模拟前端接收到的文本增量。"""
    received_deltas.append(text)
    print(f"[DELTA] {repr(text)}")

# 测试场景 1：纯文本回复（无工具调用）
print("\n=== 测试场景 1：纯文本回复 ===")
received_deltas.clear()
# 这里需要模拟 LLM 返回纯文本的情况
# 由于我们没有真实的 LLM，只能验证逻辑是否正确

# 测试场景 2：包含工具调用的回复
print("\n=== 测试场景 2：包含工具调用 ===")
received_deltas.clear()
# 这里需要模拟 LLM 返回工具调用的情况

print("\nTest script is ready")
print("Note: This script only verifies module import and basic structure. Actual functionality requires a complete LLM environment for testing.")
