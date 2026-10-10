/**
 * useStorage — 桌宠侧 storage_* WebSocket 命令封装。
 *
 * v0.4.0 §13.13 — 桌宠 / TUI gateway / Electron 三处同接入 storage_search /
 * export / import / rewind 等 6+ 能力。本文件提供 Electron 桌宠侧的
 * 类型化封装，对齐后端 spirit/desktop/ws_storage_handlers.py 的 8 个
 * handler 名（storage_search / storage_history / storage_undo /
 * storage_stats / storage_health / storage_vacuum / storage_prune /
 * storage_export）。
 *
 * 用法：
 *   const storage = useStorage()
 *   const result = await storage.search('fox')
 *   // → { ok: boolean, hits?: MessageHit[], error?: string, ... }
 *
 * 协议对齐：
 *   - 请求：  { type: 'command', action: 'storage_xxx', id, data: <params> }
 *   - 响应：  { type: 'response', action: 'storage_xxx', id, data: <cmd result> }
 *
 * 数据契约来自 spirit/cli/storage_commands.py 的 cmd_search / cmd_history
 * / cmd_undo / cmd_stats / cmd_health / cmd_vacuum / cmd_prune / cmd_export。
 */

import { useWebSocket } from './useWebSocket'

// ── 类型契约（与 spirit/cli/storage_commands.py 对齐） ─────────────

export interface MessageHit {
  session_id: string
  message_id: number
  role: string
  preview: string
  timestamp?: number
}

export interface SearchResult {
  ok: boolean
  query?: string
  hits?: MessageHit[]
  count?: number
  error?: string
}

export interface TimelineEntry {
  // 由 spirit/storage/timeline.py 的 build_timeline 决定；保留 unknown-fallback
  [key: string]: any
}

export interface HistoryResult {
  ok: boolean
  session_id?: string
  summary?: Record<string, any>
  entries?: TimelineEntry[]
  error?: string
}

export interface UndoResult {
  ok: boolean
  outcome?: string
  session_id?: string
  error?: string
}

export interface StatsResult {
  ok: boolean
  version?: string | number
  stats?: Record<string, any>
  health?: Record<string, any>
  error?: string
}

export interface HealthResult {
  ok: boolean
  snapshot?: Record<string, any>
  error?: string
}

export interface VacuumResult {
  ok: boolean
  result?: Record<string, any>
  error?: string
}

export interface PruneResult {
  ok: boolean
  operations?: Record<string, any>[]
  error?: string
}

export interface ExportResult {
  ok: boolean
  info?: Record<string, any>
  error?: string
}

// ── useStorage ───────────────────────────────────────────────────

export function useStorage() {
  const ws = useWebSocket()

  /** Internal: 调一次 storage_xxx 命令并返回 cmd_* 业务 dict。 */
  async function call<T = any>(action: string, params: Record<string, any> = {}): Promise<T> {
    return ws.send(action, params) as Promise<T>
  }

  return {
    /** FTS5 + LIKE 全文检索 messages。 */
    search(query: string, options: { limit?: number; session_id?: string } = {}): Promise<SearchResult> {
      return call<SearchResult>('storage_search', {
        query,
        limit: options.limit ?? 20,
        session_id: options.session_id,
      })
    },

    /** 取一个 session 的 timeline + summary。 */
    history(session_id: string, options: { limit?: number } = {}): Promise<HistoryResult> {
      return call<HistoryResult>('storage_history', {
        session_id,
        limit: options.limit ?? 50,
      })
    },

    /** Rewind session 到指定 user message 前（to_message 二选一）。 */
    undo(
      session_id: string,
      options: { to_message?: number; to_content?: string } = {}
    ): Promise<UndoResult> {
      return call<UndoResult>('storage_undo', {
        session_id,
        to_message: options.to_message,
        to_content: options.to_content,
      })
    },

    /** DB 结构 + 健康快照。 */
    stats(): Promise<StatsResult> {
      return call<StatsResult>('storage_stats', {})
    },

    /** PRAGMA integrity_check + schema version 快照。 */
    health(): Promise<HealthResult> {
      return call<HealthResult>('storage_health', {})
    },

    /** VACUUM（无 target_path）或 VACUUM INTO target_path。 */
    vacuum(target_path?: string): Promise<VacuumResult> {
      return call<VacuumResult>('storage_vacuum', { target_path })
    },

    /** 清理 N 天前的旧 session + orphan messages。 */
    prune(options: { older_than_days?: number; include_orphans?: boolean } = {}): Promise<PruneResult> {
      return call<PruneResult>('storage_prune', {
        older_than_days: options.older_than_days ?? 90,
        include_orphans: options.include_orphans ?? true,
      })
    },

    /** 导出 sessions + messages 到 JSON 文件。 */
    export(output_path: string): Promise<ExportResult> {
      return call<ExportResult>('storage_export', { output_path })
    },
  }
}

/** 8 个 storage_* action 名常量（与 spirit/desktop/ws_storage_handlers.py 对齐）。 */
export const STORAGE_ACTIONS = [
  'storage_search',
  'storage_history',
  'storage_undo',
  'storage_stats',
  'storage_health',
  'storage_vacuum',
  'storage_prune',
  'storage_export',
] as const

export type StorageAction = typeof STORAGE_ACTIONS[number]
