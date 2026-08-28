from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .conversations import ensure_conversation, touch_conversation
from .db import connect, now

TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"queued", "running"}
STAGES = ("planner", "generator", "validator", "finalizer")
LEASE_SECONDS = 45


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def decode_run(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    item = dict(row)
    for key in ("request_json", "response_json", "error_json", "usage_json"):
        try:
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
        except (TypeError, ValueError):
            item[key.removesuffix("_json")] = {}
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    return item


def get_run(run_id: str) -> dict[str, Any]:
    with connect() as db:
        return decode_run(db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())


def create_run(*, run_id: str, request: dict[str, Any], intent: str, conversation_id: str,
               parent_run_id: str, revision: int, mode: str, idempotency_key: str) -> dict[str, Any]:
    with connect() as db:
        if idempotency_key:
            existing = db.execute("SELECT * FROM agent_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                return decode_run(existing)
        if conversation_id:
            active = db.execute(
                "SELECT id FROM agent_runs WHERE conversation_id=? AND status IN ('queued','running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active:
                raise ValueError("conversation already has an active run")
        db.execute(
            "INSERT INTO agent_runs (id,intent,request_json,response_json,status,error_json,engine,latency_ms,created_at,conversation_id,parent_run_id,revision,mode,stage,idempotency_key,attempt,lease_owner,lease_expires_at,heartbeat_at,started_at,finished_at,cancel_requested,progress,usage_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, intent, json.dumps(request, ensure_ascii=False), "{}", "queued", "{}", "", None, now(), conversation_id, parent_run_id, revision, mode, "queued", idempotency_key, 0, "", "", "", "", "", 0, 0, "{}"),
        )
        db.commit()
    ensure_conversation(conversation_id, intent[:80], "intent")
    with connect() as db:
        return decode_run(db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())


def claim_next(owner: str | None = None) -> dict[str, Any]:
    owner = owner or owner_id()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM agent_runs WHERE status='queued' OR (status='running' AND lease_expires_at<>'' AND lease_expires_at<?) ORDER BY created_at LIMIT 1",
            (now(),),
        ).fetchone()
        if not row:
            db.commit()
            return {}
        attempt = int(row["attempt"] or 0) + 1
        stamp = now()
        db.execute(
            "UPDATE agent_runs SET status='running',stage=CASE WHEN stage='queued' THEN 'planner' ELSE stage END,attempt=?,lease_owner=?,lease_expires_at=?,heartbeat_at=?,started_at=COALESCE(NULLIF(started_at,''),?) WHERE id=?",
            (attempt, owner, _iso_after(LEASE_SECONDS), stamp, stamp, row["id"]),
        )
        db.commit()
        return decode_run(db.execute("SELECT * FROM agent_runs WHERE id=?", (row["id"],)).fetchone())


def claim_run(run_id: str, owner: str, lease_seconds: int | None = None) -> dict[str, Any]:
    lease = int(lease_seconds or LEASE_SECONDS)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM agent_runs WHERE id=? AND status='queued'", (run_id,)).fetchone()
        if not row:
            db.commit()
            return {}
        stamp = now()
        db.execute(
            "UPDATE agent_runs SET status='running', stage='planner', attempt=attempt+1, "
            "lease_owner=?, lease_expires_at=?, heartbeat_at=?, started_at=COALESCE(NULLIF(started_at,''),?) "
            "WHERE id=? AND status='queued'",
            (owner, _iso_after(lease), stamp, stamp, run_id),
        )
        db.commit()
        return decode_run(db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())


def heartbeat(run_id: str, owner: str) -> bool:
    with connect() as db:
        result = db.execute(
            "UPDATE agent_runs SET lease_expires_at=?,heartbeat_at=? WHERE id=? AND status='running' AND lease_owner=?",
            (_iso_after(LEASE_SECONDS), now(), run_id, owner),
        )
        db.commit()
        return result.rowcount == 1


def is_cancelled(run_id: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT cancel_requested,status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))


def update_run(run_id: str, **fields: Any) -> None:
    allowed = {"status", "stage", "response_json", "error_json", "engine", "latency_ms", "finished_at", "progress", "usage_json", "lease_owner", "lease_expires_at", "heartbeat_at"}
    values = [(key, value) for key, value in fields.items() if key in allowed]
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key, _ in values)
    params = [value for _, value in values] + [run_id]
    with connect() as db:
        db.execute(f"UPDATE agent_runs SET {assignments} WHERE id=?", params)
        db.commit()


def finish_run(run_id: str, *, status: str, response: dict[str, Any] | None = None, error: dict[str, Any] | None = None, usage: dict[str, Any] | None = None, latency_ms: int | None = None) -> None:
    if status not in TERMINAL:
        raise ValueError(f"invalid terminal status: {status}")
    update_run(run_id, status=status, stage="completed" if status == "completed" else ("cancelled" if status == "cancelled" else "failed"), response_json=json.dumps(response or {}, ensure_ascii=False), error_json=json.dumps(error or {}, ensure_ascii=False), usage_json=json.dumps(usage or {}, ensure_ascii=False), latency_ms=latency_ms, finished_at=now(), lease_owner="", lease_expires_at="")
    run = get_run(run_id)
    touch_conversation(str(run.get("conversation_id") or ""))


def cancel_run(run_id: str) -> dict[str, Any]:
    with connect() as db:
        db.execute("UPDATE agent_runs SET cancel_requested=1 WHERE id=? AND status IN ('queued','running')", (run_id,))
        db.execute("UPDATE agent_runs SET status='cancelled',stage='cancelled',finished_at=? WHERE id=? AND status IN ('queued','running')", (now(), run_id))
        db.commit()
        return decode_run(db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())


def append_event(run_id: str, event: dict[str, Any]) -> None:
    with connect() as db:
        sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM agent_events WHERE run_id=?", (run_id,)).fetchone()[0])
        db.execute(
            "INSERT INTO agent_events (id,run_id,sequence,event_type,tool_name,arguments_json,result_json,status,latency_ms,error_json,created_at,step_id,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), run_id, sequence, str(event.get("event_type") or ""), str(event.get("tool_name") or ""), json.dumps(event.get("arguments") or {}, ensure_ascii=False), json.dumps(event.get("result") or {}, ensure_ascii=False), str(event.get("status") or ""), event.get("latency_ms"), json.dumps(event.get("error") or {}, ensure_ascii=False), now(), str(event.get("step_id") or event.get("stage") or ""), int(event.get("attempt") or 0)),
        )
        db.commit()


def list_events(run_id: str, after: int = 0) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM agent_events WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, after)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("arguments_json", "result_json", "error_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(item.pop(key) or "{}")
            except (TypeError, ValueError):
                item[key.removesuffix("_json")] = {}
        # Keep both names in the public event contract for clients migrating
        # from step_id to the stage-oriented runtime terminology.
        item["stage"] = item.get("stage") or item.get("step_id") or ""
        items.append(item)
    return items


def recover_expired() -> int:
    with connect() as db:
        result = db.execute("UPDATE agent_runs SET status='queued',lease_owner='',lease_expires_at='',heartbeat_at='' WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? AND cancel_requested=0 AND attempt<3", (now(),))
        db.execute("UPDATE agent_runs SET status='failed',stage='failed',error_json=?,finished_at=?,lease_owner='',lease_expires_at='' WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? AND cancel_requested=0 AND attempt>=3", (json.dumps({"code": "run_lease_expired", "message": "Run lease expired after the maximum recovery attempts."}), now(), now()))
        db.commit()
        return result.rowcount
