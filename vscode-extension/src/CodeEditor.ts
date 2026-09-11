/**
 * CodeEditor — 代码编辑提案应用器。
 *
 * 接收后端 Agent 的编辑提案（EditProposal），在 VSCode 中：
 * 1. 打开文件
 * 2. 展示 diff 预览（使用 VSCode 内置 diff editor）
 * 3. 提供 Accept / Reject 按钮
 * 4. 将用户决定回传后端
 * 5. 支持 Undo
 */

import * as vscode from "vscode";
import { SpiritClient } from "./SpiritClient";

// ---------------------------------------------------------------------------
// 类型定义
// ---------------------------------------------------------------------------

interface EditProposalEvent {
  type: "edit_proposal";
  edit_id: string;
  file_path: string;
  original_content: string;
  new_content: string;
  description: string;
}

interface EditResponse {
  type: "edit_response";
  edit_id: string;
  accepted: boolean;
  message: string;
}

// ---------------------------------------------------------------------------
// CodeEditor 处理器
// ---------------------------------------------------------------------------

export class CodeEditorHandler {
  private disposables: vscode.Disposable[] = [];
  private pendingProposals = new Map<string, {
    originalUri: vscode.Uri;
    modifiedUri: vscode.Uri;
  }>();

  constructor(private readonly client: SpiritClient) {
    // 监听来自后端的编辑提案
    this.client.onEvent((event) => {
      if (event.type === "edit_proposal") {
        this.handleProposal(event as any);
      }
    });
  }

  /**
   * 处理编辑提案。
   */
  private async handleProposal(proposal: EditProposalEvent): Promise<void> {
    const { edit_id, file_path, original_content, new_content, description } = proposal;

    try {
      // 创建临时文件用于 diff 展示
      const originalUri = vscode.Uri.parse(
        `spirit-edit:original-${edit_id}.txt`
      );
      const modifiedUri = vscode.Uri.parse(
        `spirit-edit:modified-${edit_id}.txt`
      );

      // 注册虚拟文档内容提供者
      this.registerContentProvider(originalUri, original_content);
      this.registerContentProvider(modifiedUri, new_content);

      // 存储提案信息
      this.pendingProposals.set(edit_id, { originalUri, modifiedUri });

      // 展示 diff 编辑器
      const fileName = file_path.split(/[\\/]/).pop() || file_path;
      const title = description
        ? `Spirit: ${fileName} — ${description}`
        : `Spirit: ${fileName} — 编辑预览`;

      await vscode.commands.executeCommand(
        "vscode.diff",
        originalUri,
        modifiedUri,
        title
      );

      // 显示接受/拒绝按钮
      const accept = "Accept (应用修改)";
      const reject = "Reject (拒绝修改)";
      const choice = await vscode.window.showInformationMessage(
        `是否应用对 ${fileName} 的修改？${description ? ` (${description})` : ""}`,
        { modal: true },
        accept,
        reject
      );

      const accepted = choice === accept;

      if (accepted) {
        // 应用修改到实际文件
        await this.applyEdit(file_path, new_content);
      }

      // 回传结果到后端
      const response: EditResponse = {
        type: "edit_response",
        edit_id,
        accepted,
        message: accepted ? "用户已接受修改" : "用户已拒绝修改",
      };
      this.client.sendRaw(response);

      // 清理
      this.pendingProposals.delete(edit_id);

    } catch (e) {
      // 出错时回传拒绝
      const response: EditResponse = {
        type: "edit_response",
        edit_id,
        accepted: false,
        message: `编辑应用失败: ${e instanceof Error ? e.message : String(e)}`,
      };
      this.client.sendRaw(response);
    }
  }

  /**
   * 将新内容应用到实际文件。
   */
  private async applyEdit(filePath: string, newContent: string): Promise<void> {
    const uri = vscode.Uri.file(filePath);

    // 尝试打开已存在的文档
    let document: vscode.TextDocument;
    try {
      document = await vscode.workspace.openTextDocument(uri);
    } catch {
      // 文件不存在，创建新文件
      const data = Buffer.from(newContent, "utf-8");
      await vscode.workspace.fs.writeFile(uri, data);
      return;
    }

    // 使用 WorkspaceEdit 替换整个文档内容
    const edit = new vscode.WorkspaceEdit();
    const fullRange = new vscode.Range(
      document.positionAt(0),
      document.positionAt(document.getText().length)
    );
    edit.replace(uri, fullRange, newContent);

    const success = await vscode.workspace.applyEdit(edit);
    if (!success) {
      throw new Error("WorkspaceEdit 应用失败");
    }

    // 保存文件
    await document.save();
  }

  /**
   * 注册虚拟文档内容提供者。
   */
  private registerContentProvider(uri: vscode.Uri, content: string): void {
    const provider: vscode.TextDocumentContentProvider = {
      provideTextDocumentContent: () => content,
    };

    this.disposables.push(
      vscode.workspace.registerTextDocumentContentProvider(
        uri.scheme,
        provider
      )
    );
  }

  /**
   * 打开文件并跳转到指定行。
   */
  static async openFileAtLine(
    filePath: string,
    line: number = 0,
    character: number = 0
  ): Promise<void> {
    const uri = vscode.Uri.file(filePath);
    const document = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(document);

    const position = new vscode.Position(line, character);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(
      new vscode.Range(position, position),
      vscode.TextEditorRevealType.InCenter
    );
  }

  /**
   * 在当前光标位置插入文本。
   */
  static async insertAtCursor(text: string): Promise<boolean> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showWarningMessage("没有活动的编辑器");
      return false;
    }

    const edit = new vscode.WorkspaceEdit();
    edit.insert(editor.document.uri, editor.selection.active, text);
    return vscode.workspace.applyEdit(edit);
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
    this.pendingProposals.clear();
  }
}
