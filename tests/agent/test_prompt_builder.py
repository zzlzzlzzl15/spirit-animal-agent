"""tests/agent/test_prompt_builder.py — 系统提示词构建测试。

重点验证 Memora 知识库指导（系统默认长期记忆工具）的注入行为。
"""

from spirit.agent.prompt_builder import (
    MEMORA_GUIDANCE,
    MEMORY_GUIDANCE,
    build_system_prompt,
)


class TestMemoraGuidance:
    """Memora 知识库指导内容测试。"""

    def test_guidance_declares_default_memory_tool(self):
        """指导文本明确标注 Memora 是系统默认长期记忆工具。"""
        assert "默认长期记忆工具" in MEMORA_GUIDANCE
        assert "Memora" in MEMORA_GUIDANCE

    def test_guidance_covers_all_knowledge_tools(self):
        """四个 knowledge 工具全部在指导中列出。"""
        for tool in ("knowledge_search", "knowledge_query",
                     "knowledge_save", "knowledge_list"):
            assert tool in MEMORA_GUIDANCE

    def test_guidance_has_degradation_rule(self):
        """包含 Memora 不可用时的降级规则（不反复重试）。"""
        assert "不可用" in MEMORA_GUIDANCE
        assert "不要反复重试" in MEMORA_GUIDANCE


class TestBuildSystemPrompt:
    """build_system_prompt 注入行为测试。"""

    def test_memora_guidance_included_by_default(self):
        prompt = build_system_prompt()
        assert MEMORA_GUIDANCE in prompt

    def test_memora_guidance_can_be_disabled(self):
        prompt = build_system_prompt(include_knowledge=False)
        assert MEMORA_GUIDANCE not in prompt

    def test_memory_and_memora_coexist(self):
        """memory 工具指导与 Memora 指导同时存在且分工明确。"""
        prompt = build_system_prompt()
        assert MEMORY_GUIDANCE in prompt
        assert MEMORA_GUIDANCE in prompt

    def test_memora_in_stable_layer_before_environment(self):
        """Memora 指导位于 stable 层（环境提示之前）。"""
        prompt = build_system_prompt()
        assert prompt.index("Memora 知识库") < prompt.index("## 运行环境")
