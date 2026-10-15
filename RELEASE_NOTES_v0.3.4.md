# Spirit Agent v0.3.4 Release Notes

> 本次版本聚焦 **数据库时间戳本地化 + 显示助手撤回 + 数据迁移工具**。
> 配套 [`_migrate_utc_to_local.py`](_migrate_utc_to_local.py) 一次性脚本，处理历史 UTC 数据。

## 1. 数据库时间戳本地化（修复"spirit 时间显示少 8h"）

**问题**：Spirit 终端里**所有时间戳显示比 macOS 系统时钟早 8 小时**（CST 时区下）。

**根因**：
- SQLite `CURRENT_TIMESTAMP` **永远返回 UTC**（SQLite 规范）
- `spirit/storage/schema.py` 里 6 处表 default 用 `CURRENT_TIMESTAMP`（UTC）
- 旧 `spirit/cli/main.py` `_fmt_local_ts` 直接 `f"{started_at}"` 显示，没做时区换算
- 结果 `started_at = "2026-10-14 17:06:15"`（UTC）原样打印，用户看到以为是本地时间

**修复**：

| 文件 | 改动 |
|---|---|
| [spirit/storage/schema.py](spirit/storage/schema.py) | 6 处 `CURRENT_TIMESTAMP` → `datetime('now', 'localtime')`（sessions.started_at, messages.created_at, system_prompts.created_at, session_model_usage.updated_at, state_meta.updated_at, async_delegations.created_at） |
| [spirit/storage/sessions.py](spirit/storage/sessions.py) | `_CREATE_SESSION_SQL` 加 `started_at` 列、`create_session` 显式传 `datetime.now()`；`_END_SESSION_SQL` `ended_at` 改 localtime |
| [spirit/storage/messages.py](spirit/storage/messages.py) | `_INSERT_MESSAGE_SQL` 加 `created_at` 列（27 列）；3 个 INSERT 调用点显式传本地时间 |
| [spirit/storage/usage.py](spirit/storage/usage.py) | `_RECORD_USAGE_SQL` `updated_at = CURRENT_TIMESTAMP` → `datetime('now', 'localtime')` |

**为什么改 Python 端显式传值**：SQLite 不支持直接 `ALTER COLUMN ... SET DEFAULT`。靠 Python 端传值可以**对已存在的表立即生效**，避免破坏 FTS5 虚表 + 11 个 indexes + 3 个 triggers。

## 2. 显示助手撤回（不再 +8h）

之前 v0.3.3 调试中尝试过 `f"{started_at}"` +8h 换算方案。v0.3.4 **数据库已经是本地**了，撤回这套逻辑：

| 文件 | 改动 |
|---|---|
| [spirit/cli/main.py](spirit/cli/main.py) | `_fmt_local_ts` 改为纯裁剪函数（不再 +8h）；状态栏 `since` 计算同步改回 |

## 3. 数据迁移脚本 `_migrate_utc_to_local.py`

一次性脚本（仓库保留），给已存在的 `~/.spirit/state.db` 做 **UTC → 本地时间**迁移。

**智能判断**：用每条 session 的 `last_touched_at`（绝对 unix epoch）反推 started_at 是 UTC 还是本地，只 +8h 该 + 的行。

实测结果（典型用户库）：
  - `sessions.started_at`: 99 行 UTC 转本地，2 行本地跳过
  - `messages.created_at`: 321 行 UTC 转本地，23 行本地跳过
  - 其他表：均为空，跳过

**端到端验证**：用临时 SQLite + 新 schema，Python 端传 `01:19:58` → DB 存 `01:19:58`（不再 `17:19:58` UTC）。

## 4. 元数据

- [pyproject.toml](pyproject.toml): `0.3.3` → `0.3.4`
- [spirit/__init__.py](spirit/__init__.py): `__version__` `0.3.3` → `0.3.4`

## 5. 升级方式

```bash
git pull
pip install -e .         # 重装以同步版本号
```

**已有用户数据迁移**（重要）：

```bash
# 1. 备份当前 DB（脚本会再做一次稳妥备份）
cp ~/.spirit/state.db ~/.spirit/state.db.bak.pre-v0.3.4

# 2. 运行迁移
python3 _migrate_utc_to_local.py

# 3. 重启 spirit
# (kill 当前 spirit CLI 进程，然后重新启动)
```

回滚（如果出问题）：

```bash
cp ~/.spirit/state.db.bak.v0.3.4.HHMMSS ~/.spirit/state.db
git checkout v0.3.3
```

CLI / Electron 桌宠会自动使用新代码（重启 spirit / 桌宠进程即可）。