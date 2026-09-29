# Spirit Agent v0.2.0 发布说明

从 v0.1.0 到 v0.2.0，Spirit Agent 的主题是**长程任务可靠性与 provider 韧性**：任务不再静默结束、不再被超时误杀、某家 API 挂了自动换一家，并且把"跑测试"从任务流程中彻底移除。

## 一、对话可靠性：杜绝静默结束与超时误杀

- **空结论自愈**：思考模型可能把输出配额全耗在推理上、最终回复为空。现在会注入收尾提示再推一轮（≤2 次），仍为空则合成兜底总结（含工具调用清单），任务绝不以"没有任何结论"静默收尾。
- **截断自愈**：`finish_reason=length` 不再被当作最终回答，自动注入续写提示从中断处继续（≤2 次）。
- **chat 超时治理**：默认超时 300s → 1800s（修复多工具长任务被腰斩）；超时后**自动续跑**——中断当前轮、注入续跑指令从断点继续（≤3 轮），前端显示 `⏳ 自动续跑第 N/M 轮`；由 `chat.auto_resume_on_timeout / chat.max_auto_resumes / chat.resume_grace_seconds` 配置。
- **前端收尾 error-first**：CLI 终端收尾分支重排为错误优先——超时/错误必先显示 ✗ 错误行（修复"流式文本存在时错误被吞、静默回提示符"）；WS 请求看门狗延长至 4 小时，覆盖单轮 30min ×(1+续跑 3)+宽限的总时长。

## 二、跨 provider 模型切换（Hermes fallback 链语义）

- **fallback 链**：`llm.failover.providers` 优先级列表即切换链；timeout / 5xx / 过载 / 限流 / 认证 / 计费 / 模型不存在等 provider 侧失败触发沿链切换。
- **零探活 + 跳过逻辑**：激活不发探测请求（切过去仍失败则再分类再走下一家，天然"哪个能用用哪个"）；跳过曾标记不可用、字段不全、与当前后端重复的条目。
- **turn 作用域**：每轮新对话开头恢复主 provider 并重置链索引——主家恢复了自动用回去，持续不可用则每轮再切；切换通知 `⚠ <原因> — 已切换 fallback 提供方 X（模型 Y）` 推送到终端保证可见。
- 未启用 failover 时行为与单 provider 完全一致。

## 三、Provider / Transport 注册表化

- `transports/factory` 改为注册表驱动：base_url hostname 反查 + `api_mode → transport` 自动注册；声明一个 ProviderProfile 即自动获得 transport 路由，无需再改 factory（修复 `create_transport("minimax")` 抛错）。

## 四、任务工作流治理

- **系统提示词硬规则**（`TASK_WORKFLOW_RULES`）：Spirit 的任务流程禁止运行 pytest/任何测试套件、禁止新建测试文件/诊断脚本/临时验证脚本、禁止把写测试当必经步骤；验证方式 = 直接运行开发的功能或产物本身；确有必要跑测试套件须先征得用户同意。
- **planner 模板**：功能开发/修复/重构/部署模板移除硬编码"测试/测试验证"子任务，验证步骤改为"直接运行相关功能确认生效（不跑测试套件）"。

## 五、新能力与工程改进

- **CLI `/profile`**：多实例隔离 slash 命令（list / create / use / delete / rename / describe / info），`use` 即时切换 SPIRIT_HOME。
- **桌宠常驻自主循环**：launcher 挂载 Phase 7 autonomy 常驻线程（HITL 气泡询问 + 沙箱约束），`--no-autonomy` 可禁用。
- **checkpoint 重构**：工具层薄包装化，真实实现提升至 `spirit/checkpoint/`——内容寻址 blob 去重 store、SPIRIT_HOME 感知、无导入期副作用、新增 diff / cleanup / stats 动作。
- 版本号全仓对齐 **0.2.0**（`desktop-app/package.json`、`spirit/desktop/ws_server.py`、`spirit/__init__.py`、`pyproject.toml`、`vscode-extension/package.json`、CLI banner）。

## 升级注意

- 新增配置键（`chat.*`、`llm.failover`）均为可选，缺省行为即推荐行为。
- 桌宠形态需重启桌面应用使新提示词与切换逻辑生效。
