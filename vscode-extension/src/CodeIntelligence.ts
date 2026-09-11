/**
 * CodeIntelligence — 代码智能处理器。
 *
 * 接收后端 Agent 的代码智能请求，调用 VSCode 内置命令执行，
 * 将结果序列化回后端。
 *
 * 支持的命令：
 * - go_to_definition → vscode.executeDefinitionProvider
 * - find_references → vscode.executeReferenceProvider
 * - get_hover_info → vscode.executeHoverProvider
 * - get_diagnostics → languages.getDiagnostics
 * - workspace_symbols → vscode.executeWorkspaceSymbolProvider
 * - get_document_symbols → vscode.executeDocumentSymbolProvider
 */

import * as vscode from "vscode";
import { SpiritClient } from "./SpiritClient";

// ---------------------------------------------------------------------------
// 类型定义
// ---------------------------------------------------------------------------

interface CodeIntelligenceRequest {
  request_id: string;
  command: string;
  params: Record<string, any>;
}

interface CodeIntelligenceResponse {
  type: "code_intelligence_response";
  request_id: string;
  result?: any;
  error?: string;
}

// ---------------------------------------------------------------------------
// CodeIntelligence 处理器
// ---------------------------------------------------------------------------

export class CodeIntelligenceHandler {
  private disposables: vscode.Disposable[] = [];

  constructor(private readonly client: SpiritClient) {
    // 监听来自后端的请求（通过 WebSocket 事件）
    this.client.onEvent((event) => {
      if (event.type === "code_intelligence_request") {
        this.handleRequest(event as any);
      }
    });
  }

  /**
   * 处理来自后端的代码智能请求。
   */
  private async handleRequest(req: CodeIntelligenceRequest): Promise<void> {
    const { request_id, command, params } = req;
    let response: CodeIntelligenceResponse;

    try {
      const result = await this.executeCommand(command, params);
      response = {
        type: "code_intelligence_response",
        request_id,
        result,
      };
    } catch (e) {
      response = {
        type: "code_intelligence_response",
        request_id,
        error: e instanceof Error ? e.message : String(e),
      };
    }

    // 通过 WebSocket 发回结果
    this.client.sendRaw(response);
  }

  /**
   * 执行 VSCode 代码智能命令。
   */
  private async executeCommand(
    command: string,
    params: Record<string, any>
  ): Promise<any> {
    switch (command) {
      case "go_to_definition":
        return this.goToDefinition(params);
      case "find_references":
        return this.findReferences(params);
      case "get_hover_info":
        return this.getHoverInfo(params);
      case "get_diagnostics":
        return this.getDiagnostics(params);
      case "workspace_symbols":
        return this.workspaceSymbols(params);
      case "get_document_symbols":
        return this.getDocumentSymbols(params);
      default:
        throw new Error(`未知命令: ${command}`);
    }
  }

  // ------------------------------------------------------------------
  // 各命令实现
  // ------------------------------------------------------------------

  /**
   * 跳转到定义。
   */
  private async goToDefinition(params: Record<string, any>): Promise<any> {
    const uri = this.resolveUri(params.file);
    const position = this.resolvePosition(params);
    if (!uri || !position) {
      throw new Error("需要指定文件路径和位置");
    }

    const locations = await vscode.commands.executeCommand<vscode.Location[]>(
      "vscode.executeDefinitionProvider",
      uri,
      position
    );

    if (!locations || locations.length === 0) {
      return { found: false, message: "未找到定义" };
    }

    return {
      found: true,
      definitions: locations.map((loc: vscode.Location) => ({
        file: loc.uri.fsPath,
        line: loc.range.start.line,
        character: loc.range.start.character,
        endLine: loc.range.end.line,
        endCharacter: loc.range.end.character,
      })),
    };
  }

  /**
   * 查找引用。
   */
  private async findReferences(params: Record<string, any>): Promise<any> {
    const uri = this.resolveUri(params.file);
    const position = this.resolvePosition(params);
    if (!uri || !position) {
      throw new Error("需要指定文件路径和位置");
    }

    const locations = await vscode.commands.executeCommand<vscode.Location[]>(
      "vscode.executeReferenceProvider",
      uri,
      position
    );

    if (!locations || locations.length === 0) {
      return { found: false, count: 0, references: [] };
    }

    return {
      found: true,
      count: locations.length,
      references: locations.map((loc: vscode.Location) => ({
        file: loc.uri.fsPath,
        line: loc.range.start.line,
        character: loc.range.start.character,
        endLine: loc.range.end.line,
        endCharacter: loc.range.end.character,
      })),
    };
  }

  /**
   * 获取悬停信息。
   */
  private async getHoverInfo(params: Record<string, any>): Promise<any> {
    const uri = this.resolveUri(params.file);
    const position = this.resolvePosition(params);
    if (!uri || !position) {
      throw new Error("需要指定文件路径和位置");
    }

    const hovers = await vscode.commands.executeCommand<vscode.Hover[]>(
      "vscode.executeHoverProvider",
      uri,
      position
    );

    if (!hovers || hovers.length === 0) {
      return { found: false, message: "无悬停信息" };
    }

    // 提取文本内容
    const contents: string[] = [];
    for (const hover of hovers) {
      for (const content of hover.contents) {
        if (typeof content === "string") {
          contents.push(content);
        } else if ("value" in content) {
          contents.push(content.value);
        }
      }
    }

    return {
      found: true,
      content: contents.join("\n\n"),
    };
  }

  /**
   * 获取诊断信息。
   */
  private async getDiagnostics(params: Record<string, any>): Promise<any> {
    const file = params.file;

    if (file) {
      // 特定文件的诊断
      const uri = this.resolveUri(file);
      if (!uri) {
        throw new Error("无法解析文件路径");
      }

      const diagnostics = vscode.languages.getDiagnostics(uri);
      return {
        file,
        count: diagnostics.length,
        diagnostics: diagnostics.map((d: vscode.Diagnostic) => ({
          line: d.range.start.line,
          character: d.range.start.character,
          endLine: d.range.end.line,
          endCharacter: d.range.end.character,
          severity: this.severityToString(d.severity),
          source: d.source || "unknown",
          message: d.message,
          code: d.code ? String(d.code) : undefined,
        })),
      };
    } else {
      // 所有文件的诊断概要
      const allDiagnostics = vscode.languages.getDiagnostics();
      const summary: { file: string; errors: number; warnings: number }[] = [];

      for (const [uri, diagnostics] of allDiagnostics) {
        if (diagnostics.length === 0) continue;
        let errors = 0;
        let warnings = 0;
        for (const d of diagnostics) {
          if (d.severity === vscode.DiagnosticSeverity.Error) errors++;
          else if (d.severity === vscode.DiagnosticSeverity.Warning) warnings++;
        }
        summary.push({
          file: uri.fsPath,
          errors,
          warnings,
        });
      }

      return { summary: summary.slice(0, 50) };
    }
  }

  /**
   * 工作区符号搜索。
   */
  private async workspaceSymbols(params: Record<string, any>): Promise<any> {
    const query = params.query || "";
    if (!query) {
      throw new Error("请提供搜索关键词");
    }

    const symbols = await vscode.commands.executeCommand<vscode.SymbolInformation[]>(
      "vscode.executeWorkspaceSymbolProvider",
      query
    );

    if (!symbols || symbols.length === 0) {
      return { found: false, count: 0, symbols: [] };
    }

    return {
      found: true,
      count: symbols.length,
      symbols: symbols.slice(0, 50).map((s: vscode.SymbolInformation) => ({
        name: s.name,
        kind: vscode.SymbolKind[s.kind],
        containerName: s.containerName,
        file: s.location.uri.fsPath,
        line: s.location.range.start.line,
        character: s.location.range.start.character,
      })),
    };
  }

  /**
   * 获取文件符号列表。
   */
  private async getDocumentSymbols(params: Record<string, any>): Promise<any> {
    const uri = this.resolveUri(params.file);
    if (!uri) {
      throw new Error("需要指定文件路径");
    }

    const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
      "vscode.executeDocumentSymbolProvider",
      uri
    );

    if (!symbols || symbols.length === 0) {
      return { found: false, count: 0, symbols: [] };
    }

    return {
      found: true,
      count: symbols.length,
      symbols: this.flattenSymbols(symbols),
    };
  }

  // ------------------------------------------------------------------
  // 辅助方法
  // ------------------------------------------------------------------

  private resolveUri(filePath: string): vscode.Uri | null {
    if (!filePath) return null;
    try {
      return vscode.Uri.file(filePath);
    } catch {
      return null;
    }
  }

  private resolvePosition(params: Record<string, any>): vscode.Position | null {
    if (params.line === undefined || params.character === undefined) return null;
    return new vscode.Position(params.line, params.character);
  }

  private severityToString(severity: vscode.DiagnosticSeverity): string {
    switch (severity) {
      case vscode.DiagnosticSeverity.Error:
        return "error";
      case vscode.DiagnosticSeverity.Warning:
        return "warning";
      case vscode.DiagnosticSeverity.Information:
        return "info";
      case vscode.DiagnosticSeverity.Hint:
        return "hint";
      default:
        return "unknown";
    }
  }

  /**
   * 将嵌套的 DocumentSymbol 树扁平化。
   */
  private flattenSymbols(
    symbols: vscode.DocumentSymbol[],
    depth: number = 0
  ): any[] {
    const result: any[] = [];
    for (const s of symbols) {
      result.push({
        name: s.name,
        kind: vscode.SymbolKind[s.kind],
        detail: s.detail,
        line: s.range.start.line,
        endLine: s.range.end.line,
        depth,
      });
      if (s.children && s.children.length > 0) {
        result.push(...this.flattenSymbols(s.children, depth + 1));
      }
    }
    return result;
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }
}
