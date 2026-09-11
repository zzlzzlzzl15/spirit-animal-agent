"""Spirit Agent 工具系统。

导入所有工具模块以触发自动注册到 ToolRegistry。
"""

# 核心文件工具
from spirit.tools import file_tools
from spirit.tools import file_operations
from spirit.tools import search_tools
from spirit.tools import read_extract
from spirit.tools import execute_code

# 终端工具
from spirit.tools import terminal_tool
from spirit.tools import terminal_ext

# Web 工具
from spirit.tools import web_tools

# 项目工具
from spirit.tools import project_tools

# 任务/记忆
from spirit.tools import todo_tool
from spirit.tools import memory_tool

# 交互工具
from spirit.tools import clarify_tool
from spirit.tools import write_approval

# 会话/检查点
from spirit.tools import checkpoint
from spirit.tools import session_search
from spirit.tools import delegate_tool
from spirit.tools import file_state

# 图像分析
from spirit.tools import image_analysis

# 基础设施
from spirit.tools import infra_utils

# 浏览器自动化
from spirit.tools import browser

# 媒体生成
from spirit.tools import media_gen

# 语音工具
from spirit.tools import voice

# 平台集成
from spirit.tools import platforms

# 技能系统
from spirit.tools import skills

# MCP 协议
from spirit.tools import mcp_client

# 扩展工具
from spirit.tools import extra_tools

# 工具基础设施
from spirit.tools import tool_infra

# 危险命令检测与审批系统
from spirit.tools import approval

# 异步委托
from spirit.tools import async_delegation

# 网关原语（clarify/slash_confirm/hook_spill/MS Graph/tirith 等）
from spirit.tools import gateway_primitives

# 代码智能（VSCode IDE 回调模式）
from spirit.tools import code_intelligence

# 编辑提案管理（VSCode 模式下的文件编辑协议）
from spirit.tools import edit_proposal

# Memora 知识库工具
from spirit.tools import memora_tool
