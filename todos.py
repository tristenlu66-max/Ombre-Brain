"""Small persistent todo store shared by the MCP tool and HTTP API."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class TodoStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 2,
                    status TEXT NOT NULL DEFAULT 'active',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
            if "sort_order" not in columns:
                conn.execute("ALTER TABLE todos ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_todos_order "
                "ON todos(status, priority, created_at DESC)"
            )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _clean_title(title):
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        if len(title) > 200:
            raise ValueError("title is too long")
        return title

    @staticmethod
    def _priority(value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 2
        if value not in (1, 2, 3):
            raise ValueError("priority must be 1, 2, or 3")
        return value

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def list(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, priority, status, sort_order, created_at, updated_at "
                "FROM todos ORDER BY sort_order, created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def add(self, title, priority=2):
        title = self._clean_title(title)
        priority = self._priority(priority)
        now = self._now()
        item_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM todos").fetchone()[0]
            conn.execute(
                "INSERT INTO todos(id, title, priority, status, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (item_id, title, priority, next_order, now, now),
            )
        return self.get(item_id)

    def get(self, item_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title, priority, status, sort_order, created_at, updated_at "
                "FROM todos WHERE id = ?",
                (str(item_id),),
            ).fetchone()
        return self._row(row)

    def update(self, item_id, *, title=None, priority=None, status=None, sort_order=None):
        current = self.get(item_id)
        if not current:
            return None
        title = self._clean_title(title) if title is not None else current["title"]
        priority = self._priority(priority) if priority is not None else current["priority"]
        status = status if status is not None else current["status"]
        sort_order = int(sort_order) if sort_order is not None else current["sort_order"]
        if status not in ("active", "completed"):
            raise ValueError("status must be active or completed")
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE todos SET title = ?, priority = ?, status = ?, sort_order = ?, updated_at = ? "
                "WHERE id = ?",
                (title, priority, status, sort_order, now, str(item_id)),
            )
        return self.get(item_id)

    def reorder(self, ids):
        ids = [str(item_id) for item_id in ids]
        with self._connect() as conn:
            known = {row[0] for row in conn.execute("SELECT id FROM todos").fetchall()}
            if any(item_id not in known for item_id in ids) or len(set(ids)) != len(ids):
                raise ValueError("invalid todo order")
            for position, item_id in enumerate(ids):
                conn.execute("UPDATE todos SET sort_order = ?, updated_at = ? WHERE id = ?", (position, self._now(), item_id))
        return self.list()

    def delete(self, item_id):
        with self._connect() as conn:
            result = conn.execute("DELETE FROM todos WHERE id = ?", (str(item_id),))
        return result.rowcount > 0
