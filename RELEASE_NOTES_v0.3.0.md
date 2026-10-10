# Spirit Agent v0.3.0 Release Notes

> 本次版本聚焦 **Spirit Storage 向 Hermes 风格全面升级**，含完整 schema、多模块拆分、批处理持久化、in-flight transcript repair、cold-start 兼容、macOS WAL 屏障等。

## 核心变更：Spirit Storage Hermes 升级（设计文档 §G.6 / §13）

Spirit Storage 从单文件 381 行重写为 **31 个聚焦模块**（`spirit/storage/*.py`）+ **16 个 agent 层中间件**（`spirit/agent/storage_*.py`），对齐 Hermes 设计思路：

### 1. 完整 Schema（49 列 sessions / 23 列 messages / 7 关联表）
- 7 张表 `CREATE TABLE IF NOT EXISTS`：schema_version / system_prompts / sessions(49) / messages(23) / session_model_usage / state_meta / async_delegations
- `_parse_schema_columns` 模式 + `_apply_schema` 渐进补列（v0 / v0.3.0 → v0.4.0 兼容）
- 5 个 FTS5 虚表（messages_fts / messages_fts_trigram / messages_fts_cjk / sessions_fts / sessions_fts_trigram）支持全文检索
- 自动 schema-version 检测 + 旧版 db 自动备份重建

### 2. Storage 多模块拆分
| 模块 | 职责 |
|------|------|
| `spirit/storage/schema.py` | 完整 schema DDL + 渐进迁移 |
| `spirit/storage/dbfile.py` | 连接管理 / PRAGMA / 安全文件权限 |
| `spirit/storage/sessions.py` | sessions CRUD + title 设置 |
| `spirit/storage/messages.py` | 单条 / 批量 INSERT / 全文搜索 / in-flight repair |
| `spirit/storage/usage.py` | per-session / per-model token 聚合 |
| `spirit/storage/errors.py` | 错误类型 + classify_persistence_error |
| `spirit/storage/cold_start.py` | schema cold-start 兼容 + FTS holder |
| `spirit/storage/wal.py` | macOS WAL reset barrier + quarantine |
| `spirit/storage/holders.py` | session holder 锁 + TTL |
| `spirit/storage/registry.py` | 实例注册表 + generation 屏障 |
| 其余 22 个 | schema_coordination / compression / timeline / lockguard / ... |

### 3. 批处理持久化（Hermes-style intrinsic dedup）
- `_DB_PERSISTED_MARKER = "__spirit_persisted__"` dict 内禀标记（非 `id(msg)` 集合，避免 CPython 地址复用陷阱）
- `_db_flush_collect` / `_db_flush_write` 单事务 batch INSERT
- `persist_session(session_id, messages, *, stamp_after)` 主入口
- 失败分类：`StateDbCorruptError` / `StateDbReplacedError` / lock / disk_full → 不同处理路径

### 4. 16 个 agent 层中间件（facade + policy）

| 模块 | 关键能力 |
|------|----------|
| `storage_message_sanitization` | 六大子集：surrogate 修复 / tool name coerce / 非 ASCII strip / outbound kwargs / JSON closer rebalance / control char escape + secret redact + length cap |
| `storage_think_scrubber` | `StreamingThinkScrubber` + 5 类 THINK_TAG_NAMES + 跨 chunk 增量修补 |
| `storage_title_generator` | stage-1 instant deterministic + stage-2 LLM upgrade 后台线程 + 等待队列 |
| `storage_transcript_repair` | **Hermes-style in-flight repair**：blank row → in-place UPDATE / non-blank winner → adopt canonical / watermark compaction clone lookup / `sync_flushed_message_markers` after commit |
| `storage_session_persistence` | facade + ephemeral-scaffolding 过滤（7 flags）+ 走 persist_session 启用 marker |
| `storage_turn_facade` | sanitize + persist + scrub + title 一站式 save_turn |
| `storage_compression_manual` | 手动压缩管线（compress_now + 部分压缩 + 激进标志） |
| `storage_compression_reply_anchor` | 保留最近 assistant reply 不被压缩（`is_compaction_summary_message` 前缀判定） |
| `storage_manual_compression_feedback` | 压缩锁跳过描述 + 手动压缩汇总 |
| `storage_async_delegation` | 异步委派追踪 |
| `storage_compaction_observer` | 压缩观测 |
| `storage_event_bridge` | 事件总线桥接（依赖 spirit.progress_notifier 上游） |
| `storage_history_archiver` | 历史归档 |
| `storage_message_summarizer` | 消息摘要 |
| `storage_session_lifecycle` | session 生命周期管理 |
| `storage_tui_methods` | 8 个 TUI 命令 + 注册表 |

### 5. Cold-start 兼容（§13.14）
- 旧版 db 自动备份到 `state.db.v0.backup-YYYYMMDD-HHMMSS`
- FTS holder 三阶段修复（3 attempts × 60s）DROP 损坏 + 等待 init_schema 重建
- v20 / v22 legacy PK 修复 8 步 SQLite rebuild
- FTS5 虚表影子表（_data / _idx / _docsize / _config）精确枚举

### 6. macOS WAL Reset Barrier（§13.15）
- 检测 vulnerable marker（`wal-reset` / `unlink-wal` / `mid-frame` 三种）
- quarantine rename `*.quarantine-wal-<ts>` 隔离
- PRAGMA synchronous=FULL 防御
- `SessionDB.__init__` 入口 wrap（PRE-init_schema 顺序敏感）
- `SessionDB.close()` 自动 checkpoint barrier

### 7. 测试隔离与生成屏障（§13.3）
- 测试隔离：`monkeypatch config.SPIRIT_HOME` + FakeCaller LLM 打桩
- 实例 generation 屏障防止 stale 实例污染
- 独立 `_open_session_db` / `_register_instance` / `_unregister_instance` API

## 兼容性 / Breaking Changes
- 旧的 `spirit.storage.session_db.SessionDB` 单文件（381 行）已替换为 `spirit.storage` 包；提供 `spirit.storage.session_db` shim 保持向后兼容。
- 旧 db 自动备份 + 重建，**用户历史 384 session 数据已确认可丢弃**（按用户明确要求）。
- `compression` / `async_delegations` 重构保留 Spirit 现状（未迁移到 Hermes 完整模式）。

## 测试覆盖
- 端到端：所有 14 个 P0/P1/P2/P3 优先级完整闭环
- 单元：16 个中间件 import + 纯函数助手（15/16 PASS，1 个 SKIP 因上游依赖）
- 集成：ephemeral filter / persist_session marker stamp / in-flight repair 三种场景

## 文件统计
- 新增 `spirit/storage/*.py`：31 个
- 新增 `spirit/agent/storage_*.py`：16 个
- 新增中间件 `spirit/agent/hitl_channel.py` / `turn_liveness.py` / `turn_truncation.py`：3 个
- 新增 CLI `storage_commands.py` / `partial_compress.py`：2 个
- 新增 API `ws_storage_handlers.py` × 2：2 个
- 新增测试 `tests/agent/test_*.py` / `tests/api/test_*.py`：18 个
- 文档：`docs/Spirit_Storage_Hermes_完整设计_v1.md`

## 仓库
- GitHub: https://github.com/zzlzzlzzl15/spirit-animal-agent
- 分支：`master`
- 上一 tag：`v0.2.11`
- 当前 tag：`v0.3.0`
