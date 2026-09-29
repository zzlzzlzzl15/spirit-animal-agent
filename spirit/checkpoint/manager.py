"""检查点管理器 —— 内容寻址快照 / 回滚。

对标 Hermes ``tools/checkpoint_manager.py``（git shadow-repo），按 Spirit「精简可
测试子集」约定改用**内容寻址 blob 去重 store**：文件内容按 sha256 存一份，快照只
引用 blob 哈希 —— 既去重（同内容多快照共享 blob），又无 git 依赖、纯标准库、可离线测。

相比旧 ``tools/checkpoint.py`` 的改进：
- SPIRIT_HOME 感知（按调用解析，见 :mod:`spirit.checkpoint.paths`），可 monkeypatch 测试。
- **无导入期副作用**（不在 import 时建目录 / 建全局实例）。
- 保留原始绝对路径，按 sha256 去重，避免旧实现按 basename 平铺导致的同名冲突。
- 支持 ``diff``（当前 vs 快照）与 ``cleanup``（含孤儿 blob 回收）。
- 索引原子写（.tmp → replace）。
"""

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.checkpoint import paths

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CheckpointManager:
    """内容寻址的文件快照管理器。"""

    def __init__(self) -> None:
        # 无副作用：不在构造时建目录，索引延迟加载。
        self._cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # 索引读写
    # ------------------------------------------------------------------

    def _load_index(self) -> List[Dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        path = paths.index_path()
        if not path.exists():
            self._cache = []
            return self._cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cps = data.get("checkpoints", []) if isinstance(data, dict) else []
            self._cache = cps if isinstance(cps, list) else []
        except Exception as exc:
            logger.warning("检查点索引解析失败，重置为空: %s", exc)
            self._cache = []
        return self._cache

    def _save_index(self) -> bool:
        cps = self._load_index()
        path = paths.index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"schema_version": _SCHEMA_VERSION, "checkpoints": cps},
                    ensure_ascii=False, indent=2, sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception as exc:
            logger.warning("检查点索引写入失败: %s", exc)
            return False

    def invalidate_cache(self) -> None:
        """清除内存索引缓存（多进程/测试切换 SPIRIT_HOME 后调用）。"""
        self._cache = None

    # ------------------------------------------------------------------
    # blob 存储
    # ------------------------------------------------------------------

    def _store_blob(self, data: bytes) -> str:
        """把内容写入 blob store（按 sha256 去重），返回哈希。"""
        digest = _sha256(data)
        blob = paths.blob_path(digest)
        if not blob.exists():
            paths.ensure_dirs()
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, blob)
        return digest

    def _read_blob(self, digest: str) -> Optional[bytes]:
        blob = paths.blob_path(digest)
        if not blob.exists():
            return None
        try:
            return blob.read_bytes()
        except Exception as exc:
            logger.warning("读取 blob 失败 %s: %s", digest, exc)
            return None

    # ------------------------------------------------------------------
    # 快照 CRUD
    # ------------------------------------------------------------------

    def _new_id(self) -> str:
        return f"cp_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def create(
        self, file_paths: List[str], description: str = "", scope: str = "global"
    ) -> Dict[str, Any]:
        """为一组文件创建快照（保存当前内容到 blob store）。

        Args:
            file_paths: 要快照的文件绝对/相对路径列表。
            description: 人类可读描述。
            scope: 作用域标签（如工作目录），用于 list/cleanup 过滤。

        Returns:
            ``{"success": bool, "checkpoint": {...}}`` 或 ``{"success": False, "error": ...}``。
        """
        if not file_paths:
            return {"success": False, "error": "没有提供要快照的文件"}

        saved: List[Dict[str, Any]] = []
        for raw in file_paths:
            src = Path(raw).expanduser()
            try:
                src = src.resolve()
            except Exception:
                pass
            if not src.exists() or not src.is_file():
                logger.debug("跳过快照（不存在或非文件）: %s", src)
                continue
            try:
                data = src.read_bytes()
            except Exception as exc:
                logger.warning("读取文件失败，跳过 %s: %s", src, exc)
                continue
            digest = self._store_blob(data)
            saved.append({
                "path": str(src),
                "name": src.name,
                "blob": digest,
                "size": len(data),
            })

        if not saved:
            return {"success": False, "error": "没有文件被快照（均不存在或不可读）"}

        checkpoint = {
            "id": self._new_id(),
            "timestamp": time.time(),
            "description": description,
            "scope": scope,
            "files": saved,
            "file_count": len(saved),
        }
        cps = self._load_index()
        cps.append(checkpoint)
        self._save_index()
        return {"success": True, "checkpoint": checkpoint}

    def get(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        for cp in self._load_index():
            if cp.get("id") == checkpoint_id:
                return cp
        return None

    def list_checkpoints(
        self, scope: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """列出快照（新→旧）。``scope`` 非空时仅列该作用域。"""
        cps = self._load_index()
        if scope is not None:
            cps = [c for c in cps if c.get("scope") == scope]
        return list(reversed(cps[-limit:])) if limit else list(reversed(cps))

    def latest(self, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
        listed = self.list_checkpoints(scope=scope, limit=1)
        return listed[0] if listed else None

    def restore(self, checkpoint_id: str) -> Dict[str, Any]:
        """回滚到指定快照：把 blob 内容写回各文件原始路径。

        回滚前把当前版本另存为 ``<name>.pre-rollback`` 作为安全网。
        """
        cp = self.get(checkpoint_id)
        if not cp:
            return {"success": False, "error": f"快照不存在: {checkpoint_id}"}

        restored: List[str] = []
        skipped: List[str] = []
        for entry in cp.get("files", []):
            target = Path(entry["path"])
            data = self._read_blob(entry["blob"])
            if data is None:
                skipped.append(entry["path"])
                logger.warning("blob 缺失，跳过恢复: %s", entry["path"])
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    safety = target.with_suffix(target.suffix + ".pre-rollback")
                    try:
                        shutil.copy2(str(target), str(safety))
                    except Exception as exc:
                        logger.debug("安全网备份失败（继续恢复）%s: %s", target, exc)
                target.write_bytes(data)
                restored.append(str(target))
            except Exception as exc:
                logger.warning("恢复失败 %s: %s", target, exc)
                skipped.append(entry["path"])

        return {
            "success": bool(restored),
            "checkpoint_id": checkpoint_id,
            "restored_files": restored,
            "skipped_files": skipped,
            "count": len(restored),
        }

    def diff(self, checkpoint_id: str) -> Dict[str, Any]:
        """对比快照与文件当前状态：unchanged / modified / missing。"""
        cp = self.get(checkpoint_id)
        if not cp:
            return {"success": False, "error": f"快照不存在: {checkpoint_id}"}

        unchanged: List[str] = []
        modified: List[str] = []
        missing: List[str] = []
        for entry in cp.get("files", []):
            target = Path(entry["path"])
            if not target.exists():
                missing.append(entry["path"])
                continue
            try:
                cur = _sha256(target.read_bytes())
            except Exception:
                missing.append(entry["path"])
                continue
            if cur == entry["blob"]:
                unchanged.append(entry["path"])
            else:
                modified.append(entry["path"])

        return {
            "success": True,
            "checkpoint_id": checkpoint_id,
            "unchanged": unchanged,
            "modified": modified,
            "missing": missing,
            "changed": bool(modified or missing),
        }

    def delete(self, checkpoint_id: str) -> Dict[str, Any]:
        """删除一个快照（并回收其孤儿 blob）。"""
        cps = self._load_index()
        target = next((c for c in cps if c.get("id") == checkpoint_id), None)
        if not target:
            return {"success": False, "error": f"快照不存在: {checkpoint_id}"}
        self._cache = [c for c in cps if c.get("id") != checkpoint_id]
        self._save_index()
        self._gc_blobs()
        return {"success": True, "deleted": checkpoint_id}

    def cleanup(self, keep: int = 20, scope: Optional[str] = None) -> Dict[str, Any]:
        """清理旧快照，保留最近 ``keep`` 个（可按 scope）。回收孤儿 blob。"""
        cps = self._load_index()
        if scope is None:
            if len(cps) <= keep:
                return {"removed": 0, "kept": len(cps)}
            removed = cps[:-keep] if keep > 0 else list(cps)
            self._cache = cps[-keep:] if keep > 0 else []
        else:
            in_scope = [c for c in cps if c.get("scope") == scope]
            others = [c for c in cps if c.get("scope") != scope]
            if len(in_scope) <= keep:
                return {"removed": 0, "kept": len(cps)}
            removed = in_scope[:-keep] if keep > 0 else list(in_scope)
            kept_scope = in_scope[-keep:] if keep > 0 else []
            self._cache = others + kept_scope
        self._save_index()
        self._gc_blobs()
        return {"removed": len(removed), "kept": len(self._cache)}

    # ------------------------------------------------------------------
    # blob 垃圾回收
    # ------------------------------------------------------------------

    def _referenced_blobs(self) -> set:
        refs = set()
        for cp in self._load_index():
            for entry in cp.get("files", []):
                if entry.get("blob"):
                    refs.add(entry["blob"])
        return refs

    def _gc_blobs(self) -> int:
        """删除不再被任何快照引用的 blob，返回删除数。"""
        blob_dir = paths.blobs_dir()
        if not blob_dir.exists():
            return 0
        referenced = self._referenced_blobs()
        removed = 0
        for blob in blob_dir.iterdir():
            if not blob.is_file():
                continue
            if blob.name.endswith(".tmp"):
                continue
            if blob.name not in referenced:
                try:
                    blob.unlink()
                    removed += 1
                except Exception as exc:
                    logger.debug("删除孤儿 blob 失败 %s: %s", blob, exc)
        return removed

    def store_stats(self) -> Dict[str, Any]:
        """存储统计：快照数、blob 数、总字节。"""
        cps = self._load_index()
        blob_dir = paths.blobs_dir()
        blob_count = 0
        total_bytes = 0
        if blob_dir.exists():
            for blob in blob_dir.iterdir():
                if blob.is_file() and not blob.name.endswith(".tmp"):
                    blob_count += 1
                    try:
                        total_bytes += blob.stat().st_size
                    except OSError:
                        pass
        return {
            "checkpoint_count": len(cps),
            "blob_count": blob_count,
            "total_bytes": total_bytes,
        }


__all__ = ["CheckpointManager"]
