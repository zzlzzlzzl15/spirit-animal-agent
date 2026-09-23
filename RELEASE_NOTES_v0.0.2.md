# Spirit Agent v0.0.2

> Electron 桌宠 + Python Agent 后端 + 交互式 CLI 终端 · 对话循环架构对标 Hermes Agent

## ✨ 主要变更

### 🔄 对话循环（Hermes 同款）
- 重构为单主循环：分支 A（tool_calls → 执行 → 回填 → 继续）/ 分支 B（纯文本最终回答）/ 分支 C（异常分类 + 指数退避重试）
- IterationBudget 迭代预算防无限循环；tool_call 先持久化后执行，会话可恢复
- 双通道工具调用解析：原生 `tool_calls` 优先，文本 XML/JSON 回退
- 流式门控：检测 `<think>`/`<tool_call>` 标签开头即暂扣推送，防原始 JSON 泄露终端，轮次结束清理回补

### 💬 CLI 终端体验
- 工具调用内联标注：`🔧 调用工具: xxx（参数预览）` → `✓ xxx 完成 (耗时)`
- **Markdown → ANSI 逐行渲染器**：流式增量按行缓冲再格式化，根治阶梯错行；标题/加粗/行内代码/链接/列表/引用/表格/分隔线全量转换
- chat_complete 兜底渲染，最终文本零丢失

### 🦊 桌宠健壮性
- 气泡窗口非焦点化（`focusable: false` + `showInactive`），弹出**绝不打断**任何窗口的键盘输入
- 休眠恢复 / 显示器变化时强制重绘透明窗口，修复"狐狸消失"
- 恢复坐标离屏校验，越界自动回退主屏底部居中
- 宠物链路端到端：首次启动引导安装内置灵狐、`pet_switch` 广播全窗口同步、缩放以服务端 prefs 为单一真相源

### 🌉 WebSocket 桥接层
- 修复事件竞态：执行器线程 → 事件循环统一阻塞式 `run_coroutine_threadsafe`，`stream_delta` 与 `chat_complete` 严格有序

### 🧰 工具与生态
- 90+ Hermes 工具全量迁移（web_search / 文件 / shell / LSP / 知识库 / 任务管理…）
- Memora 个人知识库集成（Docker: MySQL/Redis/Qdrant/Neo4j）
- prompt caching 断点注入 + 上下文压缩引擎

## 🧪 回归测试
- `e2e_tool_loop_test.py` PASS：迭代预算 / 工具循环 / 最终总结
- `e2e_ws_event_test.py` PASS：tool_start×N / tool_complete×N / stream_delta 有序性

## 🚀 快速开始
```bash
cd desktop-app
npm install
npx vite build
npx electron .
```

完整功能说明见 [README.md](https://github.com/zzlzzlzzl15/spirit-animal-agent/blob/master/README.md)
