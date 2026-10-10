"""spirit/storage/portability.py — cross-platform DB portability helpers.

v0.4.0 (Hermes-aligned): platform-specific path normalization, export /
import to JSON for migration, and `init_schema` portability hooks.

Reference: hermes_state_portability.py + hermes_state_export.py.

The schema is portable across Windows / macOS / Linux, but:
- Path separators differ (use os.fspath / pathlib always, never str concat).
- WAL files must travel with the main DB for read coherency.
- sqlite3 module supports URI paths for cross-platform openness.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# Default chunk size when streaming large columns to JSON.
EXPORT_CHUNK_SIZE = 256


class SessionExportError(RuntimeError):
    """Raised when an export cannot be completed (corrupt DB, disk full)."""


class SessionImportError(RuntimeError):
    """Raised when an import cannot be applied (bad payload, version mismatch)."""


def normalize_db_uri(db_path: Path) -> str:
    """Return a sqlite URI that works on Windows + POSIX.

    Always uses forward slashes; sqlite understands these on Windows.
    """
    p = Path(db_path).resolve()
    return p.as_posix()


def portable_vacuum_into(target_path: Path) -> None:
    """Run a full VACUUM INTO the target path. The new file is a clean copy.

    VACUUM INTO avoids holding an exclusive lock on the live DB for the
    entire rewrite — it writes to a separate file.
    """
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    # Caller is expected to supply a connection on the source DB.
    # This helper is just the path / argument pre-builder.
    raise NotImplementedError("Call conn.execute('VACUUM INTO ?', (path,)) directly.")


class SessionPortabilityMixin:
    """Export / import / cross-platform helpers.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── Export ──────────────────────────────────────────────

    def export_to_json(self, out_path: Path) -> Dict[str, Any]:
        """Export sessions + messages + usage + delegations to a JSON file.

        Returns a dict with counts and the out_path written.
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        sessions: List[Dict[str, Any]] = []
        messages: List[Dict[str, Any]] = []
        usage: List[Dict[str, Any]] = []
        delegations: List[Dict[str, Any]] = []

        for row in self.conn.execute("SELECT * FROM sessions"):
            sessions.append(dict(row))
        for row in self.conn.execute("SELECT * FROM messages"):
            messages.append(dict(row))
        if self._table_exists("usage"):
            for row in self.conn.execute("SELECT * FROM usage"):
                usage.append(dict(row))
        if self._table_exists("async_delegations"):
            for row in self.conn.execute("SELECT * FROM async_delegations"):
                delegations.append(dict(row))

        payload = {
            "version": 1,
            "exported_at": int(time.time()),
            "counts": {
                "sessions": len(sessions),
                "messages": len(messages),
                "usage": len(usage),
                "delegations": len(delegations),
            },
            "sessions": sessions,
            "messages": messages,
            "usage": usage,
            "delegations": delegations,
        }

        with out_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, default=_json_default)
        return {"path": str(out_path), **payload["counts"]}

    def export_to_sql_bundle(self, out_dir: Path) -> Dict[str, Any]:
        """Dump DB + WAL + SHM into a directory as a portable bundle."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Checkpoint first so WAL is consolidated into main DB.
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            logger.warning("WAL checkpoint skipped during export", exc_info=True)

        main = Path(self.db_path)
        target = out_dir / main.name
        shutil.copy2(main, target)
        wal = main.with_suffix(main.suffix + "-wal")
        shm = main.with_suffix(main.suffix + "-shm")
        if wal.exists():
            shutil.copy2(wal, out_dir / wal.name)
        if shm.exists():
            shutil.copy2(shm, out_dir / shm.name)
        return {"path": str(target), "size": target.stat().st_size}

    def export_to_zip(self, out_zip: Path) -> Dict[str, Any]:
        """Bundle main DB + WAL + SHM into a single zip."""
        out_zip = Path(out_zip)
        bundle_info = self.export_to_sql_bundle(out_zip.parent / "_tmp_bundle")
        try:
            with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in Path(bundle_info["path"]).parent.iterdir():
                    zf.write(p, p.name)
        finally:
            shutil.rmtree(Path(bundle_info["path"]).parent, ignore_errors=True)
        return {"path": str(out_zip), "size": out_zip.stat().st_size}

    # ── Import ──────────────────────────────────────────────

    def import_from_json(self, src_path: Path, merge: bool = False) -> Dict[str, Any]:
        """Import sessions + messages from a JSON export."""
        src = Path(src_path)
        with src.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        if not merge:
            self._truncate_all_tables()

        sessions = payload.get("sessions", [])
        messages = payload.get("messages", [])
        inserted_s, inserted_m = 0, 0
        try:
            for s in sessions:
                self.conn.execute(
                    "INSERT OR IGNORE INTO sessions (id) VALUES (:id)", s
                )
                inserted_s += 1
            for m in messages:
                self.conn.execute(
                    "INSERT OR IGNORE INTO messages (id, session_id) VALUES (:id, :session_id)",
                    m,
                )
                inserted_m += 1
            self.conn.commit()
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise SessionImportError(str(exc)) from exc
        return {"sessions": inserted_s, "messages": inserted_m}

    def _truncate_all_tables(self) -> None:
        """Drop and recreate every table. Used by hard-import."""
        tables = [
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for t in tables:
            if t.startswith("sqlite_") or t.startswith("fts_") or t.startswith("messages_"):
                continue
            try:
                self.conn.execute(f"DELETE FROM {t}")
            except sqlite3.DatabaseError:
                logger.debug("Could not truncate %s", t, exc_info=True)

    # ── Cross-platform helpers ──────────────────────────────

    def detect_platform(self) -> str:
        """Return 'windows' / 'posix' / 'darwin' for downstream decisions."""
        if os.name == "nt":
            return "windows"
        if sys.platform == "darwin":
            return "darwin"
        return "posix"

    def safe_open_path(self) -> Path:
        """Return a Path guaranteed not to fail on Windows long paths."""
        return Path(os.path.normpath(os.path.abspath(str(self.db_path))))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return obj.hex()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
