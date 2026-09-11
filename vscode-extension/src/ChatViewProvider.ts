/**
 * ChatViewProvider — 聊天侧边栏 Webview。
 *
 * 在 VSCode 活动栏显示 Spirit Agent 聊天界面，
 * 支持 Markdown 渲染、工具调用展示、流式文本。
 */

import * as vscode from "vscode";
import { SpiritClient, StreamEvent } from "./SpiritClient";
import { WorkspaceContextCollector } from "./WorkspaceContext";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private accumulatedResponse = "";

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly client: SpiritClient,
    private readonly contextCollector?: WorkspaceContextCollector
  ) {
    // 监听后端事件
    this.client.onEvent((event) => this.handleServerEvent(event));

    // 监听上下文变化，转发到 Webview 上下文栏
    if (this.contextCollector) {
      this.contextCollector.onContextChange((ctx: any) => {
        this.postMessage({ type: "context", context: ctx });
      });
    }
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtml();

    // 接收 Webview 消息
    webviewView.webview.onDidReceiveMessage((msg: { type: string; [key: string]: any }) => {
      if (msg.type === "chat") {
        this.accumulatedResponse = "";
        // 附带当前工作区上下文
        const ctx = this.contextCollector?.collect();
        this.client.sendChat(msg.message, undefined, ctx);
      } else if (msg.type === "interrupt") {
        this.client.interrupt();
      } else if (msg.type === "newSession") {
        this.client.resetSession();
      } else if (msg.type === "editDecision") {
        // 转发编辑提案的用户决定到后端
        this.client.sendRaw({
          type: "edit_response",
          edit_id: msg.edit_id,
          accepted: msg.accepted,
          message: msg.accepted ? "用户已接受修改" : "用户已拒绝修改",
        });
      }
    });
  }

  // ------------------------------------------------------------------
  // 处理后端事件
  // ------------------------------------------------------------------

  private handleServerEvent(event: StreamEvent): void {
    if (!this.view) return;

    switch (event.type) {
      case "message_chunk":
        this.accumulatedResponse += event.text;
        this.postMessage({
          type: "streamDelta",
          text: event.text,
        });
        break;

      case "message_stop":
        this.postMessage({
          type: "streamEnd",
          fullText: this.accumulatedResponse,
          final: event.final,
        });
        break;

      case "tool_call_start":
        this.postMessage({
          type: "toolStart",
          toolName: event.tool_name,
          args: event.args,
        });
        break;

      case "tool_call_result":
        this.postMessage({
          type: "toolResult",
          toolName: event.tool_name,
          result: event.result,
          ok: event.ok,
          duration: event.duration,
        });
        break;

      case "status":
        this.postMessage({
          type: "status",
          status: event.status,
          message: event.message,
        });
        break;

      case "error":
        this.postMessage({
          type: "error",
          message: event.message,
        });
        break;

      case "session":
        this.postMessage({
          type: "session",
          sessionId: event.session_id,
        });
        break;

      case "edit_proposal":
        // 转发编辑提案到 Webview 渲染
        this.postMessage({
          type: "editProposal",
          file_path: event.file_path,
          original_content: event.original_content,
          new_content: event.new_content,
          description: event.description,
          edit_id: event.edit_id,
        });
        break;

      case "edit_result":
        // 编辑结果通知
        this.postMessage({
          type: "editResult",
          edit_id: event.edit_id,
          accepted: event.accepted,
          message: event.message,
        });
        break;
    }
  }

  private postMessage(msg: any): void {
    this.view?.webview.postMessage(msg);
  }

  // ------------------------------------------------------------------
  // HTML 模板（使用外部 CSS/JS 资源）
  // ------------------------------------------------------------------

  private getHtml(): string {
    // Webview 资源 URI
    const webview = this.view?.webview;
    if (!webview) return "";

    const cssUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "chat.css")
    );
    const jsUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "chat.js")
    );
    const nonce = this.getNonce();

    return `<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <link rel="stylesheet" href="${cssUri}">
</head>
<body>
  <div id="context-bar" style="display:none; padding:4px 8px; gap:6px; align-items:center; font-size:0.8em; border-bottom:1px solid var(--vscode-panel-border);"></div>
  <div id="messages"></div>
  <div id="input-area">
    <textarea id="input" placeholder="输入消息... (Enter 发送, Shift+Enter 换行)" rows="1"></textarea>
    <button id="send-btn">发送</button>
  </div>
  <script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
  }

  /**
   * 生成 CSP nonce。
   */
  private getNonce(): string {
    let text = "";
    const possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    for (let i = 0; i < 32; i++) {
      text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
  }
}
