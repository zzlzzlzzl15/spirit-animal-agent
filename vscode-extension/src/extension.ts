/**
 * Spirit Agent VSCode 扩展 — 主入口。
 *
 * 激活时：
 * 1. 启动/连接后端服务
 * 2. 注册 Chat 侧边栏（WebviewViewProvider）
 * 3. 注册代码补全（InlineCompletionItemProvider）
 * 4. 注册右键菜单命令
 */

import * as vscode from "vscode";
import { SpiritClient } from "./SpiritClient";
import { ChatViewProvider } from "./ChatViewProvider";
import { CompletionProvider } from "./CompletionProvider";
import { WorkspaceContextCollector } from "./WorkspaceContext";
import { CodeIntelligenceHandler } from "./CodeIntelligence";
import { CodeEditorHandler } from "./CodeEditor";
import { TerminalManager } from "./TerminalManager";

let client: SpiritClient;
let serverProcess: import("child_process").ChildProcess | null = null;
let contextCollector: WorkspaceContextCollector;
let codeIntelHandler: CodeIntelligenceHandler;
let codeEditorHandler: CodeEditorHandler;
let terminalManager: TerminalManager;

export function activate(context: vscode.ExtensionContext) {
  console.log("[Spirit Agent] 扩展已激活");

  // 配置
  const config = vscode.workspace.getConfiguration("spirit");
  const serverUrl = config.get<string>("serverUrl", "http://127.0.0.1:8765");
  const wsUrl = config.get<string>("wsUrl", "ws://127.0.0.1:8765/ws/chat");
  const autoStart = config.get<boolean>("autoStartServer", true);

  // 创建 WebSocket 客户端
  client = new SpiritClient(wsUrl);

  // 自动启动后端服务
  if (autoStart) {
    startServer(context);
  }

  // 工作区上下文收集器（必须在 ChatViewProvider 之前创建）
  contextCollector = new WorkspaceContextCollector();
  context.subscriptions.push(
    contextCollector.onContextChange((ctx) => {
      client.sendContextUpdate(ctx);
    })
  );

  // 注册 Chat 侧边栏
  const chatProvider = new ChatViewProvider(context.extensionUri, client, contextCollector);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("spirit.chat", chatProvider)
  );

  // 注册代码补全
  const completionProvider = new CompletionProvider(client);
  context.subscriptions.push(
    vscode.languages.registerInlineCompletionItemProvider(
      { scheme: "file" },
      completionProvider
    )
  );

  // 代码智能处理器
  codeIntelHandler = new CodeIntelligenceHandler(client);

  // 代码编辑提案处理器
  codeEditorHandler = new CodeEditorHandler(client);

  // 终端管理器
  terminalManager = new TerminalManager(client);

  // 注册命令
  context.subscriptions.push(
    vscode.commands.registerCommand("spirit.startServer", () =>
      startServer(context)
    ),
    vscode.commands.registerCommand("spirit.stopServer", stopServer),
    vscode.commands.registerCommand("spirit.newSession", () =>
      client.resetSession()
    ),
    vscode.commands.registerCommand("spirit.askSelection", () =>
      askAboutSelection()
    ),
    vscode.commands.registerCommand("spirit.explainCode", () =>
      runOnSelection("请解释这段代码：")
    ),
    vscode.commands.registerCommand("spirit.fixCode", () =>
      runOnSelection("请修复这段代码中的问题：")
    ),
    vscode.commands.registerCommand("spirit.refactorCode", () =>
      runOnSelection("请重构这段代码，改善可读性和性能：")
    ),
    vscode.commands.registerCommand("spirit.insertAtCursor", () =>
      insertAtCursor()
    ),
    vscode.commands.registerCommand("spirit.openFile", () =>
      openFileFromChat()
    )
  );

  // 状态栏
  const statusBarItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100
  );
  statusBarItem.text = "$(symbol-event) Spirit";
  statusBarItem.tooltip = "Spirit Agent";
  statusBarItem.command = "spirit.newSession";
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // 监听连接状态
  client.onStatusChange((status) => {
    if (status === "connected") {
      statusBarItem.text = "$(symbol-event) Spirit $(check)";
      statusBarItem.backgroundColor = undefined;
    } else if (status === "disconnected") {
      statusBarItem.text = "$(symbol-event) Spirit $(warning)";
      statusBarItem.backgroundColor = new vscode.ThemeColor(
        "statusBarItem.warningBackground"
      );
    }
  });

  console.log("[Spirit Agent] 扩展初始化完成");
}

export function deactivate() {
  stopServer();
  terminalManager?.dispose();
  codeEditorHandler?.dispose();
  codeIntelHandler?.dispose();
  contextCollector?.dispose();
  client?.dispose();
}

// ---------------------------------------------------------------------------
// 内部函数
// ---------------------------------------------------------------------------

function startServer(context: vscode.ExtensionContext) {
  if (serverProcess) {
    vscode.window.showInformationMessage("Spirit 服务已在运行中");
    return;
  }

  const { spawn } = require("child_process");
  const pythonCmd = process.platform === "win32" ? "python" : "python3";

  serverProcess = spawn(pythonCmd, ["-m", "spirit.api.server"], {
    cwd: context.extensionPath,
    stdio: "pipe",
  });

  serverProcess.stdout?.on("data", (data: Buffer) => {
    console.log("[Spirit Server]", data.toString().trim());
  });

  serverProcess.stderr?.on("data", (data: Buffer) => {
    console.error("[Spirit Server]", data.toString().trim());
  });

  serverProcess.on("exit", (code: number | null) => {
    console.log("[Spirit Server] 进程退出:", code);
    serverProcess = null;
  });

  // 等待服务启动后连接 WebSocket
  setTimeout(() => {
    client.connect();
  }, 2000);

  vscode.window.showInformationMessage("Spirit 服务已启动");
}

function stopServer() {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
    vscode.window.showInformationMessage("Spirit 服务已停止");
  }
}

function askAboutSelection() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return;

  const selection = editor.selection;
  const text = editor.document.getText(selection);
  if (!text) {
    vscode.window.showWarningMessage("请先选择代码");
    return;
  }

  vscode.window
    .showInputBox({ prompt: "你想问什么？", placeHolder: "例如：这段代码的时间复杂度是？" })
    .then((question: string | undefined) => {
      if (question) {
        const message = `${question}\n\n\`\`\`${editor.document.languageId}\n${text}\n\`\`\``;
        client.sendChat(message);
      }
    });
}

function runOnSelection(prefix: string) {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return;

  const selection = editor.selection;
  const text = editor.document.getText(selection);
  if (!text) {
    vscode.window.showWarningMessage("请先选择代码");
    return;
  }

  const message = `${prefix}\n\n\`\`\`${editor.document.languageId}\n${text}\n\`\`\``;
  client.sendChat(message);
}

function insertAtCursor() {
  // 由 CodeEditorHandler 静态方法处理
  vscode.window.showInformationMessage("使用 Spirit 聊天中的代码块操作来插入代码");
}

function openFileFromChat() {
  // 通过 QuickOpen 打开文件
  vscode.commands.executeCommand("workbench.action.quickOpen");
}
