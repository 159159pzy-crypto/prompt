"""Conversation list, search, rename, pin, and hard-delete helpers."""
from __future__ import annotations

import json
from typing import Any

from .db import connect, now


def ensure_conversation(conversation_id: str, title: str, title_source: str = "intent") -> None:
    if not conversation_id:
        return
    title = (title or "")[:80]
    stamp = now()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO conversations (id, title, title_source, pinned, archived_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (conversation_id, title, title_source, 0, "", stamp, stamp),
        )
        db.commit()


def touch_conversation(conversation_id: str) -> None:
    if not conversation_id:
        return
    with connect() as db:
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), conversation_id))
        db.commit()


def get_conversation(conversation_id: str) -> dict[str, Any]:
    items = list_conversations(conversation_id=conversation_id)["items"]
    return items[0] if items else {}


def _variant_count(response_json: str) -> int:
    try:
        payload = json.loads(response_json or "{}")
    except (TypeError, ValueError):
        return 0
    variants = payload.get("variants") if isinstance(payload, dict) else None
    return len(variants) if isinstance(variants, list) else 0


def _like(query: str) -> str:
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_conversations(*, q: str = "", limit: int = 20, offset: int = 0, conversation_id: str = "") -> dict[str, Any]:
    query = str(q or "").strip()
    like = _like(query) if query else ""
    where = ["c.archived_at = ''"]
    params: list[Any] = []
    if conversation_id:
        where.append("c.id = ?")
        params.append(conversation_id)
    if query:
        where.append("(c.title LIKE ? ESCAPE '\\' OR IFNULL(r.intent, '') LIKE ? ESCAPE '\\')")
        params.extend([like, like])
    where_sql = " AND ".join(where)
    with connect() as db:
        total = db.execute(
            f"""
            SELECT COUNT(*) FROM conversations c
            LEFT JOIN agent_runs r ON r.id = (
                SELECT id FROM agent_runs
                WHERE conversation_id = c.id
                ORDER BY revision DESC, created_at DESC
                LIMIT 1
            )
            WHERE {where_sql}
            """,
            params,
        ).fetchone()[0]
        rows = db.execute(
            f"""
            SELECT c.id, c.title, c.pinned, c.title_source, c.updated_at, c.created_at,
                   r.id AS latest_run_id,
                   r.status AS latest_status,
                   r.revision AS latest_revision,
                   r.intent AS latest_intent,
                   r.response_json AS latest_response_json,
                   (SELECT COUNT(*) FROM agent_runs x WHERE x.conversation_id = c.id) AS revision_count
            FROM conversations c
            LEFT JOIN agent_runs r ON r.id = (
                SELECT id FROM agent_runs
                WHERE conversation_id = c.id
                ORDER BY revision DESC, created_at DESC
                LIMIT 1
            )
            WHERE {where_sql}
            ORDER BY c.pinned DESC, c.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["pinned"] = bool(item.get("pinned"))
        item["variant_count"] = _variant_count(item.pop("latest_response_json", "") or "")
        item["revision_count"] = int(item.get("revision_count") or 0)
        item["latest_revision"] = int(item.get("latest_revision") or 0)
        items.append(item)
    return {"items": items, "total": int(total)}


def list_conversation_runs(conversation_id: str) -> dict[str, Any]:
    with connect() as db:
        exists = db.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not exists:
            return {}
        rows = db.execute(
            "SELECT id, revision, status, created_at, intent, mode, response_json, error_json FROM agent_runs WHERE conversation_id=? ORDER BY revision ASC",
            (conversation_id,),
        ).fetchall()
    items = []
    for row in rows:
        try:
            error = json.loads(row["error_json"] or "{}")
        except (TypeError, ValueError):
            error = {}
        items.append({
            "id": row["id"],
            "revision": int(row["revision"] or 1),
            "status": row["status"],
            "created_at": row["created_at"],
            "intent": row["intent"],
            "mode": row["mode"],
            "variant_count": _variant_count(row["response_json"] or ""),
            "error": error or None,
        })
    return {"items": items}


def patch_conversation(conversation_id: str, *, title: str | None = None, pinned: bool | None = None) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return {}
        fields: list[str] = []
        params: list[Any] = []
        if title is not None:
            fields.extend(["title=?", "title_source=?"])
            params.extend([title[:80], "user"])
        if pinned is not None:
            fields.append("pinned=?")
            params.append(int(bool(pinned)))
        if fields:
            fields.append("updated_at=?")
            params.append(now())
            params.append(conversation_id)
            db.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id=?", params)
            db.commit()
    return get_conversation(conversation_id)


def delete_conversation(conversation_id: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return False
        db.execute("DELETE FROM agent_runs WHERE conversation_id=?", (conversation_id,))
        db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        db.commit()
        return True
