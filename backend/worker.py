from __future__ import annotations

import asyncio
import json
import logging
import msvcrt
import time
from typing import Any

from . import app
from .db import init_db, now
from .orchestrator import run_pipeline
from .run_store import append_event, claim_next, finish_run, heartbeat, is_cancelled, owner_id, recover_expired, update_run

logger = logging.getLogger(__name__)


def _acquire_worker_lock() -> Any:
    from .db import DATA
    DATA.mkdir(exist_ok=True)
    handle = open(DATA / "worker.lock", "a+", encoding="ascii")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(owner_id())
    handle.flush()
    return handle


def _request_body(payload: dict[str, Any]) -> Any:
    return app.GenerateIn.model_validate(payload)


async def execute_run(run: dict[str, Any], owner: str | None = None) -> dict[str, Any]:
    owner = owner or owner_id()
    run_id = run["id"]
    payload = run.get("request") or {}
    body = _request_body(payload)
    provider = app._provider(body.provider_id or str(app._runtime_settings().get("provider_id") or ""))
    secret = app._provider_secret(provider) if provider else ""
    runtime = app._runtime_settings()
    skills = {item["id"]: True for item in app.skill_catalog({})}
    skills.update(dict(runtime.get("skills") or {}))
    skills["__intent"] = body.intent
    skills["__explicit_skill_ids"] = list((body.current_document or {}).get("_explicit_skill_ids") or [])
    skills["__selected_skill_ids"] = []
    from .skills import selected_ids
    skills["__selected_skill_ids"] = selected_ids(skills)
    started = time.perf_counter()

    def emit(event: dict[str, Any]) -> None:
        event.setdefault("attempt", run.get("attempt", 0))
        append_event(run_id, event)
        if event.get("stage"):
            update_run(run_id, stage=str(event["stage"]), progress={"planner": 10, "generator": 55, "validator": 80, "finalizer": 100}.get(str(event["stage"]), 0))

    try:
        pipeline_task = asyncio.create_task(run_pipeline(body, provider, secret, str(runtime.get("system_prompt") or ""), skills, event_sink=emit, cancel_check=lambda: is_cancelled(run_id)))
        while not pipeline_task.done():
            if is_cancelled(run_id):
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except asyncio.CancelledError:
                    pass
                cancelled_error = {"code": "run_cancelled", "message": "Run 已取消。"}
                finish_run(run_id, status="cancelled", error=cancelled_error, usage={"latency_ms": int((time.perf_counter() - started) * 1000)}, latency_ms=int((time.perf_counter() - started) * 1000))
                return {"status": "cancelled", "error": cancelled_error}
            await asyncio.sleep(0.25)
        result = await pipeline_task
        if result.get("status") == "cancelled":
            finish_run(run_id, status="cancelled", error=result.get("error"), usage={"latency_ms": result.get("latency_ms")}, latency_ms=result.get("latency_ms"))
        elif result.get("status") == "completed":
            response = {"id": run_id, **result, "conversation_id": run.get("conversation_id", ""), "revision": run.get("revision", 1), "parent_run_id": run.get("parent_run_id", ""), "mode": run.get("mode", "create"), "provider_id": body.provider_id, "model": body.model, "reasoning_effort": body.reasoning_effort}
            finish_run(run_id, status="completed", response=response, usage=result.get("usage") or {}, latency_ms=result.get("latency_ms"))
        else:
            finish_run(run_id, status="failed", error=result.get("error"), usage={"latency_ms": result.get("latency_ms")}, latency_ms=result.get("latency_ms"))
        return result
    except Exception as exc:
        logger.exception("run %s failed", run_id)
        error = {"code": "worker_failed", "message": str(exc)[:500]}
        finish_run(run_id, status="failed", error=error, usage={"latency_ms": int((time.perf_counter() - started) * 1000)}, latency_ms=int((time.perf_counter() - started) * 1000))
        return {"status": "failed", "error": error}


async def run_loop(poll_seconds: float = 0.5) -> None:
    init_db()
    owner = owner_id()
    recover_expired()
    while True:
        run = claim_next(owner)
        if not run:
            await asyncio.sleep(poll_seconds)
            continue
        await execute_run(run, owner)
        heartbeat(run["id"], owner)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    lock = _acquire_worker_lock()
    if lock is None:
        logger.info("another worker already owns the local worker lock")
        return
    try:
        asyncio.run(run_loop())
    finally:
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock.close()


if __name__ == "__main__":
    main()
