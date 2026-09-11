/**
 * WorkspaceContext — 工作区上下文收集器。
 *
 * 监听 VSCode 编辑器状态变化，收集当前工作区信息：
 * - workspaceRoot: 工作区根目录
 * - activeFile: 当前活动文件路径 + 语言
 * - selection: 选中的代码
 * - cursorPosition: 光标位置
 * - openTabs: 当前打开的标签页
 *
 * 通过防抖机制避免快速切换时洪水式推送。
 */

import * as vscode from "vscode";

// ---------------------------------------------------------------------------
// 数据结构
// ---------------------------------------------------------------------------

export interface EditorContext {
  /** 工作区根目录 */
  workspaceRoot: string | null;
  /** 当前活动文件路径 */
  activeFile: string | null;
  /** 当前文件语言 ID */
  languageId: string | null;
  /** 选中的代码 */
  selection: string | null;
  /** 选中范围（起始行-结束行） */
  selectionRange: { startLine: number; endLine: number } | null;
  /** 光标位置 */
  cursorPosition: { line: number; character: number } | null;
  /** 当前打开的标签页文件路径列表 */
  openTabs: string[];
  /** 光标所在行的内容 */
  currentLine: string | null;
}

export type ContextChangeCallback = (ctx: EditorContext) => void;

// ---------------------------------------------------------------------------
// WorkspaceContext 收集器
// ---------------------------------------------------------------------------

export class WorkspaceContextCollector {
  private disposables: vscode.Disposable[] = [];
  private changeCallbacks: ContextChangeCallback[] = [];
  private debounceTimer: NodeJS.Timeout | null = null;
  private debounceMs = 200;
  private lastContext: EditorContext | null = null;

  constructor() {
    // 监听活动编辑器变化
    this.disposables.push(
      vscode.window.onDidChangeActiveTextEditor(() => {
        this.scheduleNotify();
      })
    );

    // 监听选区变化
    this.disposables.push(
      vscode.window.onDidChangeTextEditorSelection(() => {
        this.scheduleNotify();
      })
    );

    // 监听文档变化（用户输入）
    this.disposables.push(
      vscode.workspace.onDidChangeTextDocument(() => {
        this.scheduleNotify();
      })
    );

    // 监听标签页变化
    this.disposables.push(
      vscode.window.tabGroups.onDidChangeTabs(() => {
        this.scheduleNotify();
      })
    );
  }

  /**
   * 获取当前工作区上下文的快照。
   */
  collect(): EditorContext {
    const editor = vscode.window.activeTextEditor;
    const document = editor?.document;

    // 工作区根
    const workspaceFolders = vscode.workspace.workspaceFolders;
    const workspaceRoot =
      workspaceFolders && workspaceFolders.length > 0
        ? workspaceFolders[0].uri.fsPath
        : null;

    // 活动文件
    const activeFile = document ? document.uri.fsPath : null;
    const languageId = document ? document.languageId : null;

    // 选区
    let selection: string | null = null;
    let selectionRange: { startLine: number; endLine: number } | null = null;
    if (editor && !editor.selection.isEmpty) {
      selection = document!.getText(editor.selection);
      selectionRange = {
        startLine: editor.selection.start.line,
        endLine: editor.selection.end.line,
      };
    }

    // 光标位置
    let cursorPosition: { line: number; character: number } | null = null;
    if (editor) {
      cursorPosition = {
        line: editor.selection.active.line,
        character: editor.selection.active.character,
      };
    }

    // 打开的标签页
    const openTabs: string[] = [];
    for (const tab of vscode.window.tabGroups.all.flatMap((g: vscode.TabGroup) => g.tabs)) {
      if (tab.input && typeof tab.input === "object" && "uri" in tab.input) {
        const uri = (tab.input as { uri: vscode.Uri }).uri;
        if (uri.scheme === "file") {
          openTabs.push(uri.fsPath);
        }
      }
    }

    // 当前行内容
    let currentLine: string | null = null;
    if (editor && document) {
      const line = editor.selection.active.line;
      if (line < document.lineCount) {
        currentLine = document.lineAt(line).text;
      }
    }

    return {
      workspaceRoot,
      activeFile,
      languageId,
      selection,
      selectionRange,
      cursorPosition,
      openTabs,
      currentLine,
    };
  }

  /**
   * 注册上下文变化回调。
   */
  onContextChange(cb: ContextChangeCallback): vscode.Disposable {
    this.changeCallbacks.push(cb);
    return {
      dispose: () => {
        this.changeCallbacks = this.changeCallbacks.filter((c) => c !== cb);
      },
    };
  }

  /**
   * 防抖调度通知。
   */
  private scheduleNotify(): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
    }
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      this.notify();
    }, this.debounceMs);
  }

  private notify(): void {
    const ctx = this.collect();

    // 只在上下文有实质变化时通知
    if (this.lastContext && this.isEqual(this.lastContext, ctx)) {
      return;
    }
    this.lastContext = ctx;

    for (const cb of this.changeCallbacks) {
      try {
        cb(ctx);
      } catch (e) {
        console.error("[WorkspaceContext] 回调错误:", e);
      }
    }
  }

  /**
   * 浅比较两个上下文是否相同。
   */
  private isEqual(a: EditorContext, b: EditorContext): boolean {
    return (
      a.activeFile === b.activeFile &&
      a.selection === b.selection &&
      a.cursorPosition?.line === b.cursorPosition?.line &&
      a.cursorPosition?.character === b.cursorPosition?.character &&
      a.openTabs.length === b.openTabs.length
    );
  }

  /**
   * 清理所有监听器。
   */
  dispose(): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer);
    }
    for (const d of this.disposables) {
      d.dispose();
    }
    this.disposables = [];
    this.changeCallbacks = [];
  }
}
