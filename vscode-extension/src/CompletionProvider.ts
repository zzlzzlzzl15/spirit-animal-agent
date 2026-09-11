/**
 * CompletionProvider — 代码补全（InlineCompletionItemProvider）。
 *
 * 在编辑器中提供 AI 驱动的代码补全：
 * - 检测用户暂停输入后触发
 * - 发送当前文件上下文到后端
 * - 显示 AI 生成的补全建议
 */

import * as vscode from "vscode";
import { SpiritClient } from "./SpiritClient";

export class CompletionProvider implements vscode.InlineCompletionItemProvider {
  private pendingRequest: AbortController | null = null;
  private lastTriggerTime = 0;
  private debounceMs = 300;

  constructor(private readonly client: SpiritClient) {
    // 读取配置
    const config = vscode.workspace.getConfiguration("spirit");
    this.debounceMs = config.get<number>("completionDelay", 300);
  }

  async provideInlineCompletionItems(
    document: vscode.TextDocument,
    position: vscode.Position,
    context: vscode.InlineCompletionContext,
    token: vscode.CancellationToken
  ): Promise<vscode.InlineCompletionItem[] | null> {
    // 检查是否启用
    const config = vscode.workspace.getConfiguration("spirit");
    if (!config.get<boolean>("enableCompletion", true)) {
      return null;
    }

    // 检查连接
    if (!this.client.isConnected()) {
      return null;
    }

    // 取消上一个请求
    if (this.pendingRequest) {
      this.pendingRequest.abort();
    }

    // 防抖
    const now = Date.now();
    if (now - this.lastTriggerTime < this.debounceMs) {
      return null;
    }
    this.lastTriggerTime = now;

    // 构建上下文
    const prefix = this.getContextPrefix(document, position, 2000);
    const suffix = this.getContextSuffix(document, position, 1000);
    const filePath = document.uri.fsPath;
    const languageId = document.languageId;

    // 通过 REST API 请求补全
    try {
      const serverUrl = config.get<string>("serverUrl", "http://127.0.0.1:8765");
      const response = await fetch(`${serverUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: this.buildCompletionPrompt(
            filePath,
            languageId,
            prefix,
            suffix
          ),
          session_id: this.client.currentSessionId,
        }),
        signal: token.isCancellationRequested ? AbortSignal.abort() : undefined,
      });

      if (!response.ok) return null;

      const data = await response.json();
      const completion = this.extractCompletion(data.response);

      if (!completion) return null;

      const item = new vscode.InlineCompletionItem(
        completion,
        new vscode.Range(position, position)
      );

      return [item];
    } catch (e) {
      if ((e as any).name === "AbortError") return null;
      console.error("[SpiritCompletion] 请求失败:", e);
      return null;
    }
  }

  // ------------------------------------------------------------------
  // 辅助方法
  // ------------------------------------------------------------------

  private getContextPrefix(
    document: vscode.TextDocument,
    position: vscode.Position,
    maxChars: number
  ): string {
    const startLine = Math.max(0, position.line - 50);
    const range = new vscode.Range(
      new vscode.Position(startLine, 0),
      position
    );
    const text = document.getText(range);
    return text.slice(-maxChars);
  }

  private getContextSuffix(
    document: vscode.TextDocument,
    position: vscode.Position,
    maxChars: number
  ): string {
    const endLine = Math.min(document.lineCount - 1, position.line + 30);
    const range = new vscode.Range(
      position,
      new vscode.Position(endLine, document.lineAt(endLine).text.length)
    );
    const text = document.getText(range);
    return text.slice(0, maxChars);
  }

  private buildCompletionPrompt(
    filePath: string,
    languageId: string,
    prefix: string,
    suffix: string
  ): string {
    return (
      `你是一个代码补全引擎。根据上下文，补全光标处的代码。\n` +
      `文件: ${filePath}\n语言: ${languageId}\n\n` +
      `已有代码（光标前）:\n\`\`\`${languageId}\n${prefix}\n\`\`\`\n\n` +
      `光标后代码:\n\`\`\`${languageId}\n${suffix}\n\`\`\`\n\n` +
      `请直接输出补全的代码（不要解释，不要重复已有代码，不要加 markdown 标记）:`
    );
  }

  private extractCompletion(response: string): string {
    // 清理响应：去掉 markdown 代码块标记
    let cleaned = response.trim();

    // 去掉 ```language ... ``` 包裹
    const codeBlockMatch = cleaned.match(/```[\w]*\n?([\s\S]*?)```/);
    if (codeBlockMatch) {
      cleaned = codeBlockMatch[1].trim();
    }

    // 去掉可能的解释文字（如果第一行包含"补全"、"以下是"等）
    const lines = cleaned.split("\n");
    const skipPatterns = ["补全", "以下是", "这是", "代码", "```"];
    while (
      lines.length > 0 &&
      skipPatterns.some((p) => lines[0].includes(p))
    ) {
      lines.shift();
    }

    return lines.join("\n").trim();
  }
}
