/**
 * SpiritClient — WebSocket 后端通信客户端。
 *
 * 负责：
 * - 维护与 Spirit Agent 后端的 WebSocket 连接
 * - 发送对话消息
 * - 接收流式事件
 * - 自动重连
 */

import * as vscode from "vscode";
import { EditorContext } from "./WorkspaceContext";

export interface StreamEvent {
  type: string;
  [key: string]: any;
}

export type StatusCallback = (status: "connected" | "disconnected" | "connecting") => void;
export type EventCallback = (event: StreamEvent) => void;

export class SpiritClient {
  private ws: WebSocket | null = null;
  private url: string;
  private sessionId: string | null = null;
  private statusCallbacks: StatusCallback[] = [];
  private eventCallbacks: EventCallback[] = [];
  private reconnectTimer: NodeJS.Timeout | null = null;
  private _status: "connected" | "disconnected" | "connecting" = "disconnected";

  constructor(url: string) {
    this.url = url;
  }

  // ------------------------------------------------------------------
  // 连接管理
  // ------------------------------------------------------------------

  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    this._status = "connecting";
    this.emitStatus("connecting");

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this._status = "connected";
        this.emitStatus("connected");
        console.log("[SpiritClient] 已连接:", this.url);
      };

      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data as string);
          this.handleMessage(data);
        } catch (e) {
          console.error("[SpiritClient] 解析失败:", e);
        }
      };

      this.ws.onclose = () => {
        this._status = "disconnected";
        this.emitStatus("disconnected");
        this.scheduleReconnect();
      };

      this.ws.onerror = (err) => {
        console.error("[SpiritClient] 连接错误");
        this._status = "disconnected";
        this.emitStatus("disconnected");
      };
    } catch (e) {
      console.error("[SpiritClient] 连接失败:", e);
      this.scheduleReconnect();
    }
  }

  disconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this._status = "disconnected";
    this.emitStatus("disconnected");
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 5000);
  }

  // ------------------------------------------------------------------
  // 消息发送
  // ------------------------------------------------------------------

  sendChat(message: string, model?: string, context?: EditorContext): void {
    if (!this.isConnected()) {
      vscode.window.showErrorMessage("Spirit Agent 未连接");
      return;
    }

    const payload: any = {
      type: "chat",
      message,
      session_id: this.sessionId,
    };
    if (model) payload.model = model;
    if (context) payload.context = context;

    this.ws!.send(JSON.stringify(payload));
  }

  /**
   * 主动推送工作区上下文更新（不触发对话）。
   */
  sendContextUpdate(context: EditorContext): void {
    if (!this.isConnected()) return;
    this.ws!.send(
      JSON.stringify({
        type: "context_update",
        session_id: this.sessionId,
        context,
      })
    );
  }

  /**
   * 发送原始 JSON 消息到后端（用于代码智能响应等非标准消息）。
   */
  sendRaw(data: Record<string, any>): void {
    if (!this.isConnected()) return;
    this.ws!.send(JSON.stringify(data));
  }

  resetSession(): void {
    if (this.isConnected()) {
      this.ws!.send(JSON.stringify({ type: "reset" }));
      this.sessionId = null;
    }
  }

  interrupt(): void {
    if (this.isConnected()) {
      this.ws!.send(JSON.stringify({ type: "interrupt" }));
    }
  }

  // ------------------------------------------------------------------
  // 事件处理
  // ------------------------------------------------------------------

  private handleMessage(data: StreamEvent): void {
    // 更新 session_id
    if (data.type === "session" && data.session_id) {
      this.sessionId = data.session_id;
    }

    // 分发给所有监听者
    for (const cb of this.eventCallbacks) {
      try {
        cb(data);
      } catch (e) {
        console.error("[SpiritClient] 事件回调错误:", e);
      }
    }
  }

  // ------------------------------------------------------------------
  // 事件订阅
  // ------------------------------------------------------------------

  onStatusChange(cb: StatusCallback): vscode.Disposable {
    this.statusCallbacks.push(cb);
    return {
      dispose: () => {
        this.statusCallbacks = this.statusCallbacks.filter((c) => c !== cb);
      },
    };
  }

  onEvent(cb: EventCallback): vscode.Disposable {
    this.eventCallbacks.push(cb);
    return {
      dispose: () => {
        this.eventCallbacks = this.eventCallbacks.filter((c) => c !== cb);
      },
    };
  }

  // ------------------------------------------------------------------
  // 状态查询
  // ------------------------------------------------------------------

  get status() {
    return this._status;
  }

  get currentSessionId() {
    return this.sessionId;
  }

  isConnected(): boolean {
    return this._status === "connected" && this.ws?.readyState === WebSocket.OPEN;
  }

  dispose(): void {
    this.disconnect();
    this.statusCallbacks = [];
    this.eventCallbacks = [];
  }

  // ------------------------------------------------------------------
  // 内部
  // ------------------------------------------------------------------

  private emitStatus(status: "connected" | "disconnected" | "connecting"): void {
    for (const cb of this.statusCallbacks) {
      try {
        cb(status);
      } catch {}
    }
  }
}
