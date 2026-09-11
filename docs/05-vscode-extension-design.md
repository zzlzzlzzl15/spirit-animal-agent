# Spirit Agent VSCode 扩展设计

> VSCode 扩展是 Spirit Agent 的核心前端之一，提供编辑器内 AI 辅助编程体验。

---

## 一、扩展定位

### 1.1 与其他 AI 编程助手的区别

| 特性 | Copilot | Cursor | Cline | Spirit Agent |
|------|---------|--------|-------|-------------|
| **代码补全** | ✅ 核心 | ✅ 核心 | ❌ | ✅ |
| **聊天对话** | ✅ | ✅ | ✅ | ✅ |
| **文件读写** | ❌ | ✅ | ✅ | ✅ |
| **终端执行** | ❌ | ✅ | ✅ | ✅ |
| **系统级工具** | ❌ | ❌ | ✅ | ✅ |
| **多平台同步** | ❌ | ❌ | ❌ | ✅ |
| **自托管** | ❌ | ❌ | ✅ | ✅ |

**Spirit Agent 的独特价值**：
- 编辑器内的 AI 助手 + 系统级 Agent 能力
- 在 VSCode 里操作的内容，可以在 Telegram/Web 上继续
- 自托管，数据不离开本地

---

## 二、扩展架构

### 2.1 整体结构

```
vscode-extension/
│
├── package.json              # 扩展清单
├── tsconfig.json
├── webpack.config.js         # 或 esbuild
│
├── src/
│   ├── extension.ts          # 扩展入口（activate/deactivate）
│   │
│   ├── core/                 # 核心模块
│   │   ├── SpiritClient.ts   # Spirit 后端客户端
│   │   ├── SessionManager.ts # 会话管理
│   │   └── ConfigManager.ts  # 配置管理
│   │
│   ├── chat/                 # 聊天侧边栏
│   │   ├── ChatViewProvider.ts    # Webview 提供者
│   │   ├── ChatPanel.ts           # 聊天面板逻辑
│   │   └── media/                 # 聊天 UI 资源
│   │       ├── chat.html
│   │       ├── chat.css
│   │       └── chat.js
│   │
│   ├── completion/           # 代码补全
│   │   ├── CompletionProvider.ts   # InlineCompletionItemProvider
│   │   └── Debouncer.ts            # 防抖处理
│   │
│   ├── inline/               # 内联操作
│   │   ├── CodeActionProvider.ts   # 右键菜单操作
│   │   ├── InlineEditProvider.ts   # 内联编辑（Ctrl+K）
│   │   └── DiffRenderer.ts         # Diff 渲染
│   │
│   ├── tools/                # 工具集成
│   │   ├── TerminalTool.ts        # 终端工具（VSCode 终端集成）
│   │   ├── FileTool.ts            # 文件工具（VSCode 文件系统）
│   │   └── ToolApproval.ts        # 工具审批 UI
│   │
│   └── utils/
│       ├── logger.ts
│       └── telemetry.ts
│
└── test/
    └── ...
```

### 2.2 扩展入口

```typescript
// src/extension.ts
import * as vscode from 'vscode';
import { SpiritClient } from './core/SpiritClient';
import { ChatViewProvider } from './chat/ChatViewProvider';
import { CompletionProvider } from './completion/CompletionProvider';
import { CodeActionProvider } from './inline/CodeActionProvider';

let client: SpiritClient;

export function activate(context: vscode.ExtensionContext) {
    // 1. 创建 Spirit 后端客户端
    client = new SpiritClient();
    
    // 2. 注册聊天侧边栏
    const chatProvider = new ChatViewProvider(context.extensionUri, client);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('spirit.chat', chatProvider)
    );
    
    // 3. 注册代码补全
    const completionProvider = new CompletionProvider(client);
    context.subscriptions.push(
        vscode.languages.registerInlineCompletionItemProvider(
            { pattern: '**' },
            completionProvider
        )
    );
    
    // 4. 注册右键菜单操作
    const codeActionProvider = new CodeActionProvider(client);
    context.subscriptions.push(
        vscode.languages.registerCodeActionsProvider(
            { pattern: '**' },
            codeActionProvider
        )
    );
    
    // 5. 注册命令
    context.subscriptions.push(
        vscode.commands.registerCommand('spirit.newChat', () => chatProvider.newChat()),
        vscode.commands.registerCommand('spirit.explain', () => explainSelection()),
        vscode.commands.registerCommand('spirit.refactor', () => refactorSelection()),
        vscode.commands.registerCommand('spirit.fix', () => fixSelection()),
    );
    
    // 6. 连接后端
    client.connect();
}

export function deactivate() {
    client?.disconnect();
}
```

---

## 三、核心模块设计

### 3.1 SpiritClient（后端通信）

```typescript
// src/core/SpiritClient.ts
import WebSocket from 'ws';

export class SpiritClient {
    private ws: WebSocket | null = null;
    private sessionId: string | null = null;
    private messageHandlers: Map<string, Function[]> = new Map();
    
    constructor(private backendUrl: string = 'ws://localhost:9001/ws/chat') {}
    
    // 连接后端
    async connect(): Promise<void> {
        this.ws = new WebSocket(this.backendUrl);
        
        this.ws.on('open', () => {
            console.log('Connected to Spirit backend');
            this.emit('connected');
        });
        
        this.ws.on('message', (data: string) => {
            const msg = JSON.parse(data);
            this.handleMessage(msg);
        });
        
        this.ws.on('close', () => {
            this.emit('disconnected');
            // 自动重连
            setTimeout(() => this.connect(), 3000);
        });
    }
    
    // 发送消息
    async sendMessage(content: string, metadata?: any): Promise<void> {
        if (!this.ws) throw new Error('Not connected');
        
        this.ws.send(JSON.stringify({
            type: 'user_message',
            content,
            session_id: this.sessionId,
            metadata: {
                ...metadata,
                cursor_position: this.getCursorPosition(),
                open_files: this.getOpenFiles(),
                selected_text: this.getSelectedText(),
            }
        }));
    }
    
    // 中断当前操作
    interrupt(): void {
        this.ws?.send(JSON.stringify({ type: 'interrupt' }));
    }
    
    // 审批工具执行
    approveToolCall(toolCallId: string, approved: boolean): void {
        this.ws?.send(JSON.stringify({
            type: 'tool_approve',
            tool_call_id: toolCallId,
            approved
        }));
    }
    
    // 事件监听
    on(event: string, handler: Function): void {
        if (!this.messageHandlers.has(event)) {
            this.messageHandlers.set(event, []);
        }
        this.messageHandlers.get(event)!.push(handler);
    }
    
    // 处理服务端消息
    private handleMessage(msg: any): void {
        switch (msg.type) {
            case 'session_created':
                this.sessionId = msg.session_id;
                break;
            case 'stream_delta':
                this.emit('stream_delta', msg.token);
                break;
            case 'tool_start':
                this.emit('tool_start', msg);
                break;
            case 'tool_complete':
                this.emit('tool_complete', msg);
                break;
            case 'tool_approval_required':
                this.emit('tool_approval', msg);
                break;
            case 'done':
                this.emit('done', msg);
                break;
            case 'error':
                this.emit('error', msg);
                break;
        }
    }
    
    private emit(event: string, ...args: any[]): void {
        const handlers = this.messageHandlers.get(event) || [];
        handlers.forEach(h => h(...args));
    }
    
    // VSCode 上下文获取
    private getCursorPosition() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) return null;
        return {
            line: editor.selection.active.line,
            character: editor.selection.active.character
        };
    }
    
    private getOpenFiles(): string[] {
        return vscode.workspace.textDocuments.map(doc => doc.uri.fsPath);
    }
    
    private getSelectedText(): string | null {
        const editor = vscode.window.activeTextEditor;
        if (!editor) return null;
        return editor.document.getText(editor.selection);
    }
}
```

### 3.2 聊天侧边栏

```typescript
// src/chat/ChatViewProvider.ts
import * as vscode from 'vscode';
import { SpiritClient } from '../core/SpiritClient';

export class ChatViewProvider implements vscode.WebviewViewProvider {
    private view?: vscode.WebviewView;
    private responseBuffer: string = '';
    
    constructor(
        private extensionUri: vscode.Uri,
        private client: SpiritClient
    ) {
        // 监听后端事件
        this.client.on('stream_delta', (token: string) => {
            this.responseBuffer += token;
            this.updateView();
        });
        
        this.client.on('done', () => {
            this.finalizeResponse();
        });
        
        this.client.on('tool_start', (msg: any) => {
            this.showToolProgress(msg.name, msg.args);
        });
    }
    
    resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        token: vscode.CancellationToken
    ) {
        this.view = webviewView;
        
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this.extensionUri]
        };
        
        webviewView.webview.html = this.getHtml();
        
        // 接收 Webview 消息
        webviewView.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'send') {
                this.responseBuffer = '';
                await this.client.sendMessage(msg.content);
            } else if (msg.type === 'interrupt') {
                this.client.interrupt();
            }
        });
    }
    
    newChat(): void {
        this.view?.webview.postMessage({ type: 'clear' });
        this.responseBuffer = '';
    }
    
    private updateView(): void {
        this.view?.webview.postMessage({
            type: 'append',
            content: this.responseBuffer
        });
    }
    
    private finalizeResponse(): void {
        this.view?.webview.postMessage({ type: 'finalize' });
    }
    
    private showToolProgress(name: string, args: any): void {
        this.view?.webview.postMessage({
            type: 'tool_progress',
            name,
            args
        });
    }
    
    private getHtml(): string {
        // 返回聊天 UI 的 HTML
        return `...`;
    }
}
```

### 3.3 代码补全

```typescript
// src/completion/CompletionProvider.ts
import * as vscode from 'vscode';
import { SpiritClient } from '../core/SpiritClient';

export class CompletionProvider implements vscode.InlineCompletionItemProvider {
    private lastTriggerTime: number = 0;
    private debounceMs: number = 300;
    
    constructor(private client: SpiritClient) {}
    
    async provideInlineCompletionItems(
        document: vscode.TextDocument,
        position: vscode.Position,
        context: vscode.InlineCompletionContext,
        token: vscode.CancellationToken
    ): Promise<vscode.InlineCompletionItem[]> {
        // 防抖
        const now = Date.now();
        if (now - this.lastTriggerTime < this.debounceMs) {
            return [];
        }
        this.lastTriggerTime = now;
        
        // 构建补全请求
        const prefix = document.getText(new vscode.Range(
            new vscode.Position(0, 0),
            position
        ));
        const suffix = document.getText(new vscode.Range(
            position,
            document.positionAt(document.offsetAt(document.lineAt(document.lineCount - 1).range.end))
        ));
        
        try {
            // 通过 HTTP 请求补全（不用 WebSocket，避免阻塞聊天）
            const response = await fetch('http://localhost:9001/api/v1/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    prefix,
                    suffix,
                    file_path: document.uri.fsPath,
                    language: document.languageId
                })
            });
            
            const data = await response.json();
            
            if (data.completion) {
                return [
                    new vscode.InlineCompletionItem(
                        data.completion,
                        new vscode.Range(position, position)
                    )
                ];
            }
        } catch (e) {
            // 静默失败，不影响编辑体验
        }
        
        return [];
    }
}
```

### 3.4 内联操作（右键菜单）

```typescript
// src/inline/CodeActionProvider.ts
import * as vscode from 'vscode';
import { SpiritClient } from '../core/SpiritClient';

export class CodeActionProvider implements vscode.CodeActionProvider {
    static readonly providedCodeActionKinds = [
        vscode.CodeActionKind.Refactor,
        vscode.CodeActionKind.QuickFix
    ];
    
    constructor(private client: SpiritClient) {}
    
    provideCodeActions(
        document: vscode.TextDocument,
        range: vscode.Range,
        context: vscode.CodeActionContext
    ): vscode.CodeAction[] {
        const actions: vscode.CodeAction[] = [];
        
        // 解释选中代码
        const explainAction = new vscode.CodeAction(
            'Spirit: 解释这段代码',
            vscode.CodeActionKind.Refactor
        );
        explainAction.command = {
            command: 'spirit.explain',
            title: '解释代码'
        };
        actions.push(explainAction);
        
        // 重构代码
        const refactorAction = new vscode.CodeAction(
            'Spirit: 重构这段代码',
            vscode.CodeActionKind.Refactor
        );
        refactorAction.command = {
            command: 'spirit.refactor',
            title: '重构代码'
        };
        actions.push(refactorAction);
        
        // 修复问题
        const fixAction = new vscode.CodeAction(
            'Spirit: 修复这段代码',
            vscode.CodeActionKind.QuickFix
        );
        fixAction.command = {
            command: 'spirit.fix',
            title: '修复代码'
        };
        actions.push(fixAction);
        
        return actions;
    }
}
```

---

## 四、package.json 配置

```json
{
  "name": "spirit-agent",
  "displayName": "Spirit Agent",
  "description": "AI Agent with system-level capabilities, unified across VSCode, Web, and messaging platforms",
  "version": "0.1.0",
  "publisher": "spirit-agent",
  "engines": {
    "vscode": "^1.85.0"
  },
  "categories": [
    "AI",
    "Programming Languages",
    "Other"
  ],
  "activationEvents": [
    "onStartupFinished"
  ],
  "main": "./dist/extension.js",
  "contributes": {
    "viewsContainers": {
      "activitybar": [
        {
          "id": "spirit",
          "title": "Spirit Agent",
          "icon": "media/spirit-icon.svg"
        }
      ]
    },
    "views": {
      "spirit": [
        {
          "type": "webview",
          "id": "spirit.chat",
          "name": "Chat"
        }
      ]
    },
    "commands": [
      {
        "command": "spirit.newChat",
        "title": "Spirit: New Chat"
      },
      {
        "command": "spirit.explain",
        "title": "Spirit: Explain Selection"
      },
      {
        "command": "spirit.refactor",
        "title": "Spirit: Refactor Selection"
      },
      {
        "command": "spirit.fix",
        "title": "Spirit: Fix Selection"
      }
    ],
    "menus": {
      "editor/context": [
        {
          "command": "spirit.explain",
          "when": "editorHasSelection"
        },
        {
          "command": "spirit.refactor",
          "when": "editorHasSelection"
        },
        {
          "command": "spirit.fix",
          "when": "editorHasSelection"
        }
      ]
    },
    "keybindings": [
      {
        "command": "spirit.newChat",
        "key": "ctrl+shift+s",
        "mac": "cmd+shift+s"
      }
    ],
    "configuration": {
      "title": "Spirit Agent",
      "properties": {
        "spirit.backendUrl": {
          "type": "string",
          "default": "ws://localhost:9001/ws/chat",
          "description": "Spirit backend WebSocket URL"
        },
        "spirit.enableCompletion": {
          "type": "boolean",
          "default": true,
          "description": "Enable inline code completion"
        },
        "spirit.completionDelay": {
          "type": "number",
          "default": 300,
          "description": "Completion debounce delay in ms"
        }
      }
    }
  },
  "scripts": {
    "vscode:prepublish": "npm run package",
    "compile": "webpack",
    "watch": "webpack --watch",
    "package": "webpack --mode production --devtool hidden-source-map"
  },
  "devDependencies": {
    "@types/vscode": "^1.85.0",
    "@types/node": "^20.0.0",
    "typescript": "^5.3.0",
    "webpack": "^5.89.0",
    "webpack-cli": "^5.1.0",
    "ts-loader": "^9.5.0"
  },
  "dependencies": {
    "ws": "^8.16.0"
  }
}
```

---

## 五、用户体验流程

### 5.1 典型工作流

```
1. 用户在 VSCode 中打开项目
   ↓
2. Spirit 扩展自动激活，连接后端
   ↓
3. 用户选中一段代码，右键 → "Spirit: 解释这段代码"
   ↓
4. 聊天侧边栏显示解释
   ↓
5. 用户继续问："帮我优化这段代码"
   ↓
6. Spirit 调用 write_file 工具，显示 diff 预览
   ↓
7. 用户点击 "Accept" 应用修改
   ↓
8. 用户离开电脑，在 Telegram 上继续问："刚才那个项目部署了吗？"
   ↓
9. Spirit 记得上下文，继续帮助部署
```

### 5.2 工具审批 UI

```
┌─────────────────────────────────────────────────────────┐
│  Spirit Agent 请求执行工具                              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  🔧 write_file                                          │
│                                                         │
│  路径: src/utils/helper.py                              │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ + def calculate_sum(a: int, b: int) -> int:     │   │
│  │ +     """Calculate the sum of two numbers."""   │   │
│  │ +     return a + b                              │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  [✓ Accept]  [✗ Reject]  [✓ Always Allow]              │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 六、与后端通信总结

```
VSCode 扩展                    Spirit 后端
     │                              │
     │──── WebSocket 连接 ─────────→│
     │                              │
     │──── user_message ───────────→│
     │     { content, metadata }    │
     │                              │
     │←─── session_created ─────────│
     │     { session_id }           │
     │                              │
     │←─── stream_delta ────────────│ (多次)
     │     { token }                │
     │                              │
     │←─── tool_start ──────────────│
     │     { name, args }           │
     │                              │
     │──── tool_approve ───────────→│ (如果需要)
     │     { tool_call_id, true }   │
     │                              │
     │←─── tool_complete ───────────│
     │     { name, result }         │
     │                              │
     │←─── done ────────────────────│
     │     { usage }                │
     │                              │
```

---

*下一步：[06-implementation-roadmap.md](./06-implementation-roadmap.md) — 实施路线图*
