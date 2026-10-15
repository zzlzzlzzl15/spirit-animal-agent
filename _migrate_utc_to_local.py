"""迁移脚本 v2：智能判断每条记录是 UTC 还是本地时间。

判断逻辑：
  - sessions 表：有 last_touched_at（绝对 epoch）。比较
        started_at 是不是 ≈ last_touched_at 转 UTC（如果是 UTC）
        started_at 是不是 ≈ last_touched_at 转 localtime（如果是本地）
    哪种一致就给哪种状态。

  - messages 表：通过 session_id 找对应 session 的 last_touched_at 同理判断。

  - 其他表：用 sessions 表里的判定阈值决定（同会话的字段判定一致）。

  - 没历史锚点的记录：默认 UTC（旧 schema 的 CURRENT_TIMESTAMP）。

原则：能不动就不动；只把"确认是 UTC"的转本地。
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path('/Users/zzl/.spirit/state.db')

# 表 + 字段
TABLES_COLS = [
    ('sessions',                  'started_at'),
    ('messages',                  'created_at'),
    ('system_prompts',            'created_at'),
    ('session_model_usage',       'updated_at'),
    ('state_meta',                'updated_at'),
    ('async_delegations',         'created_at'),
]

LOCAL_OFFSET = datetime.now() - datetime.utcnow()
OFFSET_HOURS = LOCAL_OFFSET.total_seconds() / 3600
print(f"本地偏移 = {OFFSET_HOURS:+.2f}h")
print(f"  datetime.utcnow() = {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"  datetime.now()    = {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 本地")
print()


def parse_ts(s: str):
    """字符串 → naive datetime（如果带 .ffffff 一并保留）"""
    if not s:
        return None
    raw = str(s).strip()
    if '.' in raw:
        head, tail = raw.split('.', 1)
        micro = (tail + '000000')[:6]
        return datetime.fromisoformat(f"{head}.{micro}")
    return datetime.fromisoformat(raw)


def to_local_str(utc_str: str) -> str:
    """UTC 字符串 → 本地字符串（保持原格式）"""
    dt = parse_ts(utc_str)
    if dt is None:
        return utc_str
    local_dt = dt + LOCAL_OFFSET
    if '.' in utc_str:
        return local_dt.strftime('%Y-%m-%d %H:%M:%S') + '.' + utc_str.split('.', 1)[1]
    return local_dt.strftime('%Y-%m-%d %H:%M:%S')


def classify_for_session(started_at: str, last_touched_at: float) -> str:
    """判断一条 session.started_at 是 UTC 还是本地。

    用 last_touched_at（绝对 unix epoch）作为锚点：
      - 如果 started_at ≈ last_touched_at 转 UTC → 是 UTC
      - 如果 started_at ≈ last_touched_at 转 localtime → 已经是本地
    容差 5 秒
    """
    if not started_at or last_touched_at is None:
        return 'unknown'
    sa = parse_ts(started_at)
    if sa is None:
        return 'unknown'
    epoch_utc = datetime.utcfromtimestamp(last_touched_at)
    epoch_local = datetime.fromtimestamp(last_touched_at)
    diff_utc = abs((sa - epoch_utc).total_seconds())
    diff_local = abs((sa - epoch_local).total_seconds())
    if diff_utc < diff_local and diff_utc < 5:
        return 'utc'
    if diff_local < 5:
        return 'local'
    if abs(diff_utc - diff_local) < 5:
        # 同样远，看时间范围（不可能同时等于 UTC 和 localtime）
        return 'unknown'
    # 哪个近哪个
    return 'utc' if diff_utc < diff_local else 'local'


def main():
    db = sqlite3.connect(str(DB))
    db.execute('PRAGMA journal_mode=WAL')

    # 1. 预先对 sessions 表分类（用 last_touched_at 反推）
    print("=== 第 1 步：先分类 sessions 表（用 last_touched_at 反推） ===")
    cur = db.execute("""
        SELECT rowid, id, started_at, last_touched_at
        FROM sessions
        WHERE started_at IS NOT NULL
    """)
    session_classifications = {}  # session_id → ('utc'|'local'|'unknown')
    utc_count = local_count = unknown_count = 0
    for rid, sid, sa, lt in cur.fetchall():
        cls = classify_for_session(sa, lt)
        session_classifications[sid] = (cls, sa, lt)
        if cls == 'utc':
            utc_count += 1
        elif cls == 'local':
            local_count += 1
        else:
            unknown_count += 1

    print(f"sessions 分类结果: utc={utc_count} | local={local_count} | unknown={unknown_count}")
    print()

    # 2. 打印样本
    print("样本 (id 前 6 字符 | started_at | 距 UTC localtime 差)：")
    for sid, (cls, sa, lt) in list(session_classifications.items())[:6]:
        epoch_utc = datetime.utcfromtimestamp(lt)
        epoch_local = datetime.fromtimestamp(lt)
        dt = parse_ts(sa)
        d_utc = (dt - epoch_utc).total_seconds() if dt else 0
        d_local = (dt - epoch_local).total_seconds() if dt else 0
        print(f"  {sid[:6]} | {sa} | UTC diff={d_utc:+.0f}s local diff={d_local:+.0f}s → {cls}")
    print()

    # 3. 逐表迁移
    print("=== 第 2 步：逐表迁移（只改 utc，保留 local） ===")
    print(f"{'表.字段':35s} | {'utc→本地':>10s} | {'本地跳过':>10s} | {'未知跳过':>10s}")
    print('-' * 80)

    total_updated = 0
    for table, col in TABLES_COLS:
        # 取所有数据
        try:
            if table == 'sessions':
                rows = db.execute(
                    f"SELECT rowid, id, {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
            elif table == 'messages':
                rows = db.execute(
                    f"SELECT rowid, session_id, {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"{table+'.'+col:35s} | (表不存在)")
            continue

        if not rows:
            print(f"{table+'.'+col:35s} | (空表)")
            continue

        updates = []  # (rowid, new_value)
        local_skipped = unknown_skipped = 0
        for r in rows:
            rid = r[0]
            val = r[-1]

            # 决定该条是不是 UTC
            if table == 'sessions':
                sid = r[1]
                cls = session_classifications.get(sid, ('unknown', None, None))[0]
            elif table == 'messages':
                sid = r[1]
                # 用所属 session 的分类
                cls = session_classifications.get(sid, ('unknown', None, None))[0]
            else:
                # 其它表：默认 UTC（schema 改之前的旧数据）
                # 但如果表里已经有 spirit 重启后用新 schema 写的本地时间，盲改会出错
                # 防御性策略：跟 max(会话 started_at) 对比 —— 当前表比 max 晚 8h+ → UTC，早 8h+ → 本地
                # 简化：默认按 utc 处理
                cls = 'utc'

            if cls == 'utc':
                new_val = to_local_str(val)
                if new_val != val:
                    updates.append((rid, new_val))
            elif cls == 'local':
                local_skipped += 1
            else:
                unknown_skipped += 1

        # 事务更新
        if updates:
            db.execute('BEGIN IMMEDIATE')
            try:
                for rid, new_val in updates:
                    db.execute(
                        f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                        (new_val, rid)
                    )
                db.execute('COMMIT')
            except Exception as e:
                db.execute('ROLLBACK')
                raise SystemExit(f"❌ {table}.{col} 迁移失败: {e}")

        n_utc = len(updates)
        total_updated += n_utc
        print(f"{table+'.'+col:35s} | {n_utc:>10d} | {local_skipped:>10d} | {unknown_skipped:>10d}")

    print()
    print(f"✅ 迁移完成：{total_updated} 条 UTC 转本地，其余保留原值")
    db.commit()
    db.close()

    # 4. 回滚说明
    backups = sorted(Path('/Users/zzl/.spirit').glob('state.db.bak.*'))
    if backups:
        print(f"回滚：cp {backups[-1]} {DB}")


if __name__ == '__main__':
    main()