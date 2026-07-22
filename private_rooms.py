"""Private room v0.1 storage and API/MCP helpers.

The private tables are intentionally isolated from all existing memory queries.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse

OWNER = "evan"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_private_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS private_room (
            room_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS private_item (
            item_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL REFERENCES private_room(room_id),
            body BLOB NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('note', 'draft', 'keepsake', 'future_letter')),
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'trash', 'destroyed')),
            remind_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_private_item_room_state
            ON private_item(room_id, state, updated_at DESC);
        """
    )
    conn.commit()
    conn.close()


def _reject(data: dict[str, Any]) -> JSONResponse | None:
    if data.get("owner") != OWNER:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def room_open_sync(db_path: str, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("owner") != OWNER:
        raise PermissionError("forbidden")
    conn = _conn(db_path)
    try:
        room_id = str(data.get("room_id") or "").strip()
        if room_id:
            row = conn.execute(
                "SELECT room_id, owner, display_name, created_at, frozen FROM private_room WHERE room_id=? AND owner=?",
                (room_id, OWNER),
            ).fetchone()
            if not row:
                raise KeyError("room_not_found")
            if row["frozen"]:
                raise ValueError("room_frozen")
            return dict(row)
        room_id = str(uuid.uuid4())
        now = _now()
        name = str(data.get("display_name") or "闺房").strip()[:200]
        conn.execute("INSERT INTO private_room(room_id,owner,display_name,created_at) VALUES(?,?,?,?)", (room_id, OWNER, name, now))
        conn.commit()
        return {"room_id": room_id, "owner": OWNER, "display_name": name, "created_at": now, "frozen": 0}
    finally:
        conn.close()


def room_put_sync(db_path: str, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("owner") != OWNER:
        raise PermissionError("forbidden")
    room_id = str(data.get("room_id") or "").strip()
    body = data.get("body")
    kind = str(data.get("kind") or "note")
    if not room_id or not isinstance(body, str) or not body:
        raise ValueError("room_id and non-empty body required")
    if kind not in {"note", "draft", "keepsake", "future_letter"}:
        raise ValueError("invalid kind")
    now = _now()
    conn = _conn(db_path)
    try:
        room = conn.execute("SELECT frozen FROM private_room WHERE room_id=? AND owner=?", (room_id, OWNER)).fetchone()
        if not room: raise KeyError("room_not_found")
        if room["frozen"]: raise ValueError("room_frozen")
        item_id = str(data.get("item_id") or uuid.uuid4())
        conn.execute("INSERT INTO private_item(item_id,room_id,body,kind,state,remind_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (item_id, room_id, body, kind, "active", data.get("remind_at"), now, now))
        conn.commit()
        return {"item_id": item_id, "room_id": room_id, "kind": kind, "state": "active", "remind_at": data.get("remind_at"), "created_at": now, "updated_at": now}
    finally: conn.close()


def room_list_sync(db_path: str, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("owner") != OWNER: raise PermissionError("forbidden")
    room_id = str(data.get("room_id") or "").strip()
    state = str(data.get("state") or "active")
    if state not in {"active", "trash", "destroyed"}: raise ValueError("invalid state")
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT item_id, room_id, body, kind, state, remind_at, created_at, updated_at FROM private_item WHERE room_id=? AND state=? ORDER BY updated_at DESC", (room_id, state)).fetchall()
        return {"room_id": room_id, "state": state, "items": [dict(r) for r in rows]}
    finally: conn.close()


def room_del_sync(db_path: str, data: dict[str, Any]) -> dict[str, Any]:
    if data.get("owner") != OWNER: raise PermissionError("forbidden")
    item_id = str(data.get("item_id") or "").strip()
    action = str(data.get("action") or "trash")
    if action not in {"trash", "destroyed", "restore"}: raise ValueError("invalid action")
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT state FROM private_item WHERE item_id=?", (item_id,)).fetchone()
        if not row: raise KeyError("item_not_found")
        if row["state"] == "destroyed": raise ValueError("destroyed_irrecoverable")
        new_state = "active" if action == "restore" else action
        conn.execute("UPDATE private_item SET state=?, updated_at=? WHERE item_id=?", (new_state, _now(), item_id))
        conn.commit()
        return {"item_id": item_id, "state": new_state}
    finally: conn.close()


def _result(fn, db_path: str, data: dict[str, Any]) -> JSONResponse:
    try: return JSONResponse(fn(db_path, data))
    except PermissionError: return JSONResponse({"error": "forbidden"}, status_code=403)
    except KeyError as e: return JSONResponse({"error": str(e).strip("'")}, status_code=404)
    except ValueError as e: return JSONResponse({"error": str(e)}, status_code=400)


async def _body(request) -> dict[str, Any]:
    try: return await request.json()
    except Exception: return {}


def make_endpoint(fn, db_path):
    async def endpoint(request):
        return _result(fn, db_path, await _body(request))
    return endpoint
