# Spirit Agent LSP 集成使用指南

## 概述

Spirit Agent 通过终端执行 shell 命令来写入文件（如 `echo "content" > file.txt`），这与 Hermes 直接在 Python 中调用 `write_file()` 不同。

本指南说明如何使用 `spirit.lsp_integration` 模块在终端命令执行前后拦截，实现 LSP 诊断功能。

## 核心功能

### 1. snapshot_before_write()

在写入文件前捕获 LSP 诊断基线。

```python
from spirit.lsp_integration import snapshot_before_write

# 执行写入命令前
snapshot_before_write("path/to/file.py")
```

### 2. get_diagnostics_after_write()

在写入文件后获取 LSP 诊断，自动应用 delta 过滤和 range shift。

```python
from spirit.lsp_integration import get_diagnostics_after_write

# 执行写入命令后
diagnostics = get_diagnostics_after_write(
    "path/to/file.py",
    pre_content=old_content,  # 写入前的内容
    post_content=new_content,  # 写入后的内容
    delta=True,  # 只返回新增的诊断
)

if diagnostics:
    print(f"发现 {len(diagnostics)} 个新错误")
```

### 3. check_write_with_lsp()

完整的 LSP 检查流程（推荐）。

```python
from spirit.lsp_integration import check_write_with_lsp

# 完整的检查流程
lsp_output = check_write_with_lsp(
    "path/to/file.py",
    pre_content=old_content,
    post_content=new_content,
)

if lsp_output:
    print(lsp_output)  # 格式化的诊断信息
```

## 在 Terminal Tool 中使用

### 方法 1：手动集成

```python
from spirit.tools.terminal_tool import terminal
from spirit.lsp_integration import snapshot_before_write, get_diagnostics_after_write

def write_file_with_lsp(path: str, content: str):
    """带 LSP 检查的文件写入。"""
    # 1. 读取旧内容
    old_content = read_file(path) if os.path.exists(path) else ""
    
    # 2. 快照基线
    snapshot_before_write(path)
    
    # 3. 执行写入命令
    result = terminal.execute(f'cat > {path}', input=content)
    
    if result.exit_code != 0:
        return f"写入失败: {result.stderr}"
    
    # 4. 获取 LSP 诊断
    lsp_diags = get_diagnostics_after_write(
        path,
        pre_content=old_content,
        post_content=content,
    )
    
    # 5. 返回结果
    if lsp_diags:
        from spirit.lsp_integration import format_diagnostics
        return f"写入成功\n{format_diagnostics(lsp_diags)}"
    else:
        return "写入成功，无 LSP 错误"
```

### 方法 2：创建专用工具

创建一个新的工具 `write_file_lsp.py`：

```python
"""带 LSP 检查的文件写入工具。"""

from spirit.tools.registry import registry
from spirit.lsp_integration import check_write_with_lsp
import os

WRITE_FILE_LSP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file_with_lsp",
        "description": (
            "写入文件并进行 LSP 诊断检查。\n"
            "如果检测到语法或语义错误，会返回详细的错误信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "文件内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}

def _read_file(path: str) -> str:
    """读取文件内容。"""
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

@registry.register(WRITE_FILE_LSP_SCHEMA)
def write_file_with_lsp(path: str, content: str) -> str:
    """写入文件并进行 LSP 检查。"""
    # 读取旧内容
    old_content = _read_file(path)
    
    # 执行写入
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        return f"写入失败: {e}"
    
    # LSP 检查
    lsp_output = check_write_with_lsp(path, old_content, content)
    
    if lsp_output:
        return f"✅ 文件已写入\n\n{lsp_output}"
    else:
        return "✅ 文件已写入，无 LSP 错误"
```

## 工作流程

```
┌─────────────────────────────────────────┐
│ 1. 读取旧内容                             │
│    old_content = read_file(path)         │
├─────────────────────────────────────────┤
│ 2. 快照基线                               │
│    snapshot_before_write(path)           │
├─────────────────────────────────────────┤
│ 3. 执行写入命令                           │
│    terminal.execute('cat > path', ...)   │
─────────────────────────────────────────┤
│ 4. 获取 LSP 诊断                          │
│    diags = get_diagnostics_after_write(  │
│        path,                             │
│        pre_content=old_content,          │
│        post_content=new_content          │
│    )                                     │
├─────────────────────────────────────────┤
│ 5. 格式化并返回                           │
│    return format_diagnostics(diags)      │
└─────────────────────────────────────────┘
```

## Delta 过滤机制

LSP 集成会自动应用以下优化：

1. **Delta Baseline** - 只报告新增的错误，不重复报告已存在的错误
2. **Range Shift** - 当文件被编辑后，自动调整诊断的行号，避免误报移动的错误
3. **Broken Set** - 失败的 LSP 服务器会被标记，后续请求直接跳过

## 配置

在 `~/.spirit/config.yaml` 中配置 LSP：

```yaml
lsp:
  enabled: true
  server_timeout: 30  # 秒
  auto_install: true  # 自动安装语言服务器
  idle_timeout: 600   # 空闲超时（秒）
```

## 支持的语言

Spirit Agent LSP 支持以下语言：

- ✅ Python (pyright)
- ✅ TypeScript/JavaScript (typescript-language-server)
- ✅ Go (gopls)
- ✅ Rust (rust-analyzer)
- ✅ YAML (yaml-language-server)
- ✅ JSON (json-language-server)
- ✅ HTML/CSS (vscode-html/css-language-server)
- ✅ Lua (lua-language-server)

## 故障排除

### Q: LSP 没有启动？

A: 检查以下几点：
1. LSP 是否启用：`lsp.enabled: true` in config
2. 是否在 Git 仓库内（LSP 需要工作区检测）
3. 语言服务器是否安装：运行 `spirit lsp install <server_id>`

### Q: 诊断延迟很高？

A: 可能原因：
1. 语言服务器首次启动需要时间（后续会复用）
2. 项目很大，索引需要时间
3. 考虑增加 `server_timeout` 配置

### Q: 如何查看 LSP 日志？

A: 日志位于 `~/.spirit/logs/spirit.log`，搜索 `lsp[` 关键字：

```bash
tail -f ~/.spirit/logs/spirit.log | grep 'lsp\['
```

## 与 Hermes 的对比

| 特性 | Hermes | Spirit |
|------|--------|--------|
| 写入方式 | Python `write_file()` | Shell 命令 `cat > file` |
| LSP 集成 | 内置在 `file_operations.py` | 独立模块 `lsp_integration.py` |
| Delta 过滤 | ✅ | ✅ |
| Range Shift | ✅ | ✅ |
| Broken Set | ✅ | ✅ |

## 下一步

- [ ] 将 `write_file_with_lsp` 工具注册到 Spirit Agent
- [ ] 在 `terminal_tool.py` 中集成 LSP 钩子
- [ ] 添加单元测试
