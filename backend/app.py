from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .agent import AGENT_TIMEOUT_SECONDS, generate as generate_agent, parse_generation_request
from .conversations import delete_conversation, get_conversation, list_conversation_runs, list_conversations, patch_conversation
from .db import DB_PATH, SCHEMA_VERSION, connect, init_db, now, row_json
from .documents import canonical_document, document_view as _document_view, export_document as _export_document, json_value as _json, lint_variant_card, snapshot as _snapshot, validate_document, write_document as _write_document
from .secrets import delete_secret, get_secret, put_secret
from . import skill_runtime
from .skill_runtime import strip_explicit_markers
from .skills import catalog as skill_catalog, discovery_diagnostics, explain_activation
from .run_store import TERMINAL, cancel_run as cancel_stored_run, claim_run, create_run, get_run, list_events, owner_id

ROOT = Path(__file__).resolve().parent.parent
PRODUCT_VERSION = "7.0.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Anima Agent Prompt Studio", version=PRODUCT_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


class TokenIn(BaseModel):
    raw_text: str
    normalized_tag: str = ""
    category: str = "Custom"
    weight: float = Field(1.0, gt=0, le=3)
    source: str = "manual"
    translation: str = ""
    locked: bool = False

    @field_validator("raw_text")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("raw_text cannot be empty")
        return value


class DocumentIn(BaseModel):
    title: str = "Untitled Anima prompt"
    intent: str = ""
    positive_tokens: list[TokenIn] = Field(default_factory=list)
    negative_tokens: list[TokenIn] = Field(default_factory=list)
    protected_tokens: list[str] = Field(default_factory=list)
    notes: str = ""
    source_run_id: str = ""
    conversation_id: str = ""
    variant_index: int = 0

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, value: str) -> str:
        return value.strip() or "Untitled Anima prompt"


class GenerateIn(BaseModel):
    intent: str
    current_document: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["create", "modify"] = "create"
    conversation_id: str = ""
    parent_run_id: str = ""
    requested_count: int | None = Field(default=None, ge=1)
    include_chinese: bool = False
    provider_id: str = ""
    model: str = ""
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"
    idempotency_key: str = ""

    @field_validator("intent")
    @classmethod
    def intent_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("intent is required")
        return value


class ProviderIn(BaseModel):
    name: str
    base_url: str
    model: str = ""
    api_key: str = ""
    env_name: str = ""
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(4096, ge=256, le=100000)
    timeout: int = Field(120, ge=1, le=300)
    enabled: bool = True

    @field_validator("name", "base_url")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field cannot be empty")
        return value

    @field_validator("base_url")
    @classmethod
    def http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        return value.rstrip("/")


class ProviderImportIn(BaseModel):
    items: list[ProviderIn] = Field(min_length=1, max_length=50)


class SkillToggleIn(BaseModel):
    enabled: bool


class ConversationPatchIn(BaseModel):
    title: str | None = None
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def title_trim(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("title cannot be empty")
        return value[:80]


def _provider_secret(row: Any) -> str:
    if not row:
        return ""
    reference = row["secret_ref"] or ""
    if reference.startswith("env:"):
        return get_secret("", env_name=reference[4:])
    if reference.startswith("ANIMA_"):
        return get_secret("", env_name=reference)
    return get_secret(reference, env_name=f"ANIMA_PROVIDER_{row['id'].replace('-', '_').upper()}")


def _provider(provider_id: str = "") -> Any:
    with connect() as db:
        if provider_id:
            return db.execute("SELECT * FROM providers WHERE id=? AND enabled=1", (provider_id,)).fetchone()
        return db.execute("SELECT * FROM providers WHERE enabled=1 ORDER BY rowid LIMIT 1").fetchone()


def _provider_view(row: Any) -> dict[str, Any]:
    item = row_json(row)
    item["models"] = _json(item.pop("models_json", "[]"), [])
    item["env_name"] = item.get("secret_ref", "")[4:] if item.get("secret_ref", "").startswith("env:") else (item.get("secret_ref", "") if item.get("secret_ref", "").startswith("ANIMA_") else "")
    item["has_api_key"] = bool(item.get("secret_ref"))
    return item


def _insert_provider(body: ProviderIn) -> str:
    provider_id = str(uuid.uuid4())
    secret_ref = f"provider-{provider_id}" if body.api_key else (f"env:{body.env_name.strip()}" if body.env_name.strip() else "")
    if body.api_key and not put_secret(secret_ref, body.api_key):
        raise HTTPException(503, "无法写入 Windows Credential Manager 或环境凭据存储")
    with connect() as db:
        db.execute(
            "INSERT INTO providers(id,name,base_url,model,temperature,max_tokens,timeout,enabled,secret_ref,models_json,models_synced_at) VALUES(?,?,?,?,?,?,?,?,?,'[]','')",
            (provider_id, body.name, body.base_url, body.model.strip(), body.temperature, body.max_tokens, body.timeout, int(body.enabled), secret_ref),
        )
    return provider_id


def _extract_models(payload: Any) -> list[str]:
    source = payload.get("data", payload.get("models", payload.get("items", []))) if isinstance(payload, dict) else payload
    if not isinstance(source, list):
        return []
    models: list[str] = []
    for item in source:
        model_id = item.get("id", item.get("name", "")) if isinstance(item, dict) else item
        if isinstance(model_id, str) and model_id.strip():
            models.append(model_id.strip())
    return sorted(set(models), key=str.casefold)


async def _sync_models(provider_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(404, "provider not found")
    secret = _provider_secret(row)
    if not secret:
        raise HTTPException(400, "未配置 API key")
    try:
        async with httpx.AsyncClient(timeout=min(float(row["timeout"]), 20.0)) as client:
            response = await client.get(row["base_url"].rstrip("/") + "/models", headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            models = _extract_models(response.json())
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise HTTPException(502, f"模型列表获取失败：{str(exc)[:300]}") from exc
    if not models:
        raise HTTPException(502, "供应商返回了空模型列表")
    selected_model = row["model"] or models[0]
    synced_at = now()
    with connect() as db:
        db.execute(
            "UPDATE providers SET model=?,models_json=?,models_synced_at=? WHERE id=?",
            (selected_model, json.dumps(models, ensure_ascii=False), synced_at, provider_id),
        )
    return {"items": models, "selected_model": selected_model, "synced_at": synced_at}


def _runtime_settings() -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT payload FROM settings WHERE key='runtime'").fetchone()
    value = _json(row["payload"] if row else "{}", {})
    if not isinstance(value, dict):
        return {}
    # Runtime is intentionally small; discard legacy/dead keys when read.
    allowed = {"requested_count", "include_chinese", "system_prompt", "provider_id", "model", "reasoning_effort", "skill_mode", "skills"}
    return {key: item for key, item in value.items() if key in allowed}


@app.get("/api/status")
def status() -> dict[str, Any]:
    with connect() as db:
        documents = db.execute("SELECT COUNT(*) FROM prompt_documents").fetchone()[0]
        provider = db.execute("SELECT COUNT(*) FROM providers WHERE enabled=1").fetchone()[0]
        queued = db.execute("SELECT COUNT(*) FROM agent_runs WHERE status='queued'").fetchone()[0]
        running = db.execute("SELECT COUNT(*) FROM agent_runs WHERE status='running'").fetchone()[0]
        conversations = db.execute("SELECT COUNT(*) FROM conversations WHERE archived_at=''").fetchone()[0]
    return {"ok": True, "name": app.title, "product": "Prompt Workbench", "version": app.version, "schema_version": SCHEMA_VERSION, "documents": documents, "enabled_providers": provider, "queued_runs": queued, "running_runs": running, "conversations": conversations, "database": str(DB_PATH)}


@app.get("/api/settings")
def settings() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT key,payload,updated_at FROM settings ORDER BY key").fetchall()
    return {"items": [{"key": row["key"], "payload": _json(row["payload"], {}), "updated_at": row["updated_at"]} for row in rows]}


@app.put("/api/settings/{key}")
def put_setting(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    with connect() as db:
        if key == "runtime":
            existing = db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
            current = _json(existing["payload"] if existing else "{}", {})
            allowed = {"requested_count", "include_chinese", "system_prompt", "provider_id", "model", "reasoning_effort", "skill_mode", "skills"}
            payload = {key: item for key, item in {**current, **payload}.items() if key in allowed}
        db.execute("INSERT OR REPLACE INTO settings(key,payload,updated_at) VALUES(?,?,?)", (key, json.dumps(payload, ensure_ascii=False), now()))
    return {"key": key, "payload": payload}


@app.get("/api/skills")
def skills(intent: str = "") -> dict[str, Any]:
    runtime = _runtime_settings()
    if intent.strip():
        explained = explain_activation(intent.strip(), runtime.get("skills") or {})
        items = [{**item, "source": "codex"} for item in explained["items"]]
        return {"items": items, "selected_skill_ids": explained["selected_skill_ids"], "diagnostics": explained["diagnostics"]}
    items = [{**item, "source": "codex"} for item in skill_catalog(runtime.get("skills"))]
    return {"items": items, "diagnostics": discovery_diagnostics()}


@app.put("/api/skills/{skill_id}")
def toggle_skill(skill_id: str, body: SkillToggleIn) -> dict[str, Any]:
    runtime = _runtime_settings()
    if skill_id not in {item["id"] for item in skill_catalog({})}:
        raise HTTPException(404, "skill not found")
    current = _runtime_settings()
    enabled = dict(current.get("skills") or {})
    enabled[skill_id] = body.enabled
    current["skills"] = enabled
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,payload,updated_at) VALUES(?,?,?)", ("runtime", json.dumps(current, ensure_ascii=False), now()))
    return next(item for item in skills()["items"] if item["id"] == skill_id)


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT * FROM providers ORDER BY rowid").fetchall()
    return {"items": [_provider_view(row) for row in rows]}


@app.post("/api/providers")
async def create_provider(body: ProviderIn) -> dict[str, Any]:
    provider_id = _insert_provider(body)
    return next(item for item in providers()["items"] if item["id"] == provider_id)


@app.post("/api/providers/import")
async def import_providers(body: ProviderImportIn) -> dict[str, Any]:
    imported = []
    for item in body.items:
        provider_id = _insert_provider(item)
        sync_error = ""
        try:
            await _sync_models(provider_id)
        except HTTPException as exc:
            sync_error = str(exc.detail)
        provider = next(row for row in providers()["items"] if row["id"] == provider_id)
        provider["model_sync_error"] = sync_error
        imported.append(provider)
    return {"items": imported}


@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderIn) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(404, "provider not found")
    if body.api_key:
        secret_ref = row["secret_ref"] if row["secret_ref"] and not row["secret_ref"].startswith(("ANIMA_", "env:")) else f"provider-{provider_id}"
    else:
        secret_ref = f"env:{body.env_name.strip()}" if body.env_name.strip() else (row["secret_ref"] or "")
    if body.api_key and not put_secret(secret_ref, body.api_key):
        raise HTTPException(503, "无法写入 Windows Credential Manager 或环境凭据存储")
    with connect() as db:
        db.execute("UPDATE providers SET name=?,base_url=?,model=?,temperature=?,max_tokens=?,timeout=?,enabled=?,secret_ref=? WHERE id=?", (body.name, body.base_url, body.model.strip(), body.temperature, body.max_tokens, body.timeout, int(body.enabled), secret_ref, provider_id))
    return next(item for item in providers()["items"] if item["id"] == provider_id)


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict[str, bool]:
    with connect() as db:
        row = db.execute("SELECT secret_ref FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            raise HTTPException(404, "provider not found")
        db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
    if row["secret_ref"] and not row["secret_ref"].startswith(("ANIMA_", "env:")):
        delete_secret(row["secret_ref"])
    return {"deleted": True}


@app.get("/api/providers/{provider_id}/models")
def provider_models(provider_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(404, "provider not found")
    return {"items": _json(row["models_json"], []), "selected_model": row["model"], "synced_at": row["models_synced_at"]}


@app.post("/api/providers/{provider_id}/models/sync")
async def sync_provider_models(provider_id: str) -> dict[str, Any]:
    return await _sync_models(provider_id)


@app.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        raise HTTPException(404, "provider not found")
    secret = _provider_secret(row)
    if not secret:
        return {"ok": False, "error": "未配置 API key"}
    try:
        async with httpx.AsyncClient(timeout=row["timeout"]) as client:
            response = await client.get(row["base_url"].rstrip("/") + "/models", headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            models = _extract_models(response.json())
        return {"ok": True, "status": response.status_code, "model_count": len(models)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}


def _prepare_run_body(body: GenerateIn) -> tuple[GenerateIn, str, str, int]:
    runtime = _runtime_settings()
    activation = skill_runtime.activate(body.intent, runtime.get("skills") or {})
    request_intent = strip_explicit_markers(body.intent)
    original_intent = request_intent
    if body.mode == "modify":
        if not body.conversation_id or not body.parent_run_id:
            raise HTTPException(400, "修改当前对话需要 conversation_id 和 parent_run_id")
        with connect() as db:
            parent = db.execute("SELECT * FROM agent_runs WHERE id=? AND conversation_id=?", (body.parent_run_id, body.conversation_id)).fetchone()
        if not parent:
            raise HTTPException(400, "当前对话版本不存在，请刷新后重试")
        original_intent = parent["intent"]
        current = dict(body.current_document or {})
        current.setdefault("original_intent", original_intent)
        current["modification_request"] = request_intent
        body = body.model_copy(update={"intent": original_intent, "current_document": current})
    else:
        body = body.model_copy(update={"intent": request_intent})
    provider_id = body.provider_id or str(runtime.get("provider_id") or "")
    provider = _provider(provider_id)
    if provider_id and not provider:
        raise HTTPException(400, "所选供应商不存在或已停用")
    if not body.model:
        runtime_model = runtime.get("model") if provider_id and provider_id == runtime.get("provider_id") else ""
        body = body.model_copy(update={"model": str(runtime_model or (provider["model"] if provider else ""))})
    fallback_count = len((body.current_document or {}).get("variants", [])) if body.mode == "modify" else 1
    parse_intent = (body.current_document or {}).get("modification_request", "") if body.mode == "modify" else body.intent
    parsed_request = parse_generation_request(parse_intent or body.intent, fallback_count=fallback_count)
    if parsed_request["explicit_count"]:
        body = body.model_copy(update={"requested_count": parsed_request["requested_count"]})
    conversation_id = body.conversation_id or str(uuid.uuid4())
    with connect() as db:
        latest = db.execute("SELECT COALESCE(MAX(revision),0) AS revision FROM agent_runs WHERE conversation_id=?", (conversation_id,)).fetchone()["revision"]
    body = body.model_copy(update={"current_document": {**(body.current_document or {}), "_explicit_skill_ids": activation["selected_skill_ids"]}})
    return body, original_intent, conversation_id, int(latest) + 1


@app.post("/api/runs", status_code=202)
async def create_agent_run(body: GenerateIn) -> dict[str, Any]:
    body, original_intent, conversation_id, revision = _prepare_run_body(body)
    run_id = str(uuid.uuid4())
    try:
        run = create_run(run_id=run_id, request=body.model_dump(), intent=original_intent, conversation_id=conversation_id, parent_run_id=body.parent_run_id, revision=revision, mode=body.mode, idempotency_key=body.idempotency_key.strip()[:200])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run_id": run["id"], "id": run["id"], "status": run["status"], "stage": run.get("stage", "queued"), "conversation_id": run["conversation_id"], "revision": run["revision"]}


@app.get("/api/runs/{run_id}")
def get_agent_run(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    response = run.get("response") if isinstance(run.get("response"), dict) else {}
    return {"run_id": run_id, "id": run_id, "status": run["status"], "stage": run.get("stage", ""), "progress": run.get("progress", 0), "conversation_id": run.get("conversation_id", ""), "revision": run.get("revision", 1), "parent_run_id": run.get("parent_run_id", ""), "result": response, "error": run.get("error") or None, "usage": run.get("usage") or {}, "attempt": run.get("attempt", 0), "cancel_requested": run.get("cancel_requested", False), "created_at": run.get("created_at"), "started_at": run.get("started_at"), "finished_at": run.get("finished_at")}


@app.get("/api/runs/{run_id}/events")
def get_agent_run_events(run_id: str, after: int = Query(0, ge=0)) -> dict[str, Any]:
    if not get_run(run_id):
        raise HTTPException(404, "run not found")
    return {"items": list_events(run_id, after=after)}


@app.post("/api/runs/{run_id}/cancel")
def cancel_agent_run(run_id: str) -> dict[str, Any]:
    if not get_run(run_id):
        raise HTTPException(404, "run not found")
    run = cancel_stored_run(run_id)
    return {"run_id": run_id, "status": run.get("status"), "cancel_requested": run.get("cancel_requested", False)}


@app.post("/api/runs/{run_id}/retry", status_code=202)
async def retry_agent_run(run_id: str) -> dict[str, Any]:
    original = get_run(run_id)
    if not original:
        raise HTTPException(404, "run not found")
    if original.get("status") not in TERMINAL:
        raise HTTPException(409, "只有已结束的 Run 才能重试")
    payload = dict(original.get("request") or {})
    payload["idempotency_key"] = str(uuid.uuid4())
    payload["parent_run_id"] = run_id
    body = GenerateIn.model_validate(payload)
    body, intent, conversation_id, revision = _prepare_run_body(body.model_copy(update={"conversation_id": original.get("conversation_id", "")}))
    run = create_run(run_id=str(uuid.uuid4()), request=body.model_dump(), intent=intent, conversation_id=conversation_id, parent_run_id=run_id, revision=revision, mode=body.mode, idempotency_key=payload["idempotency_key"])
    return {"run_id": run["id"], "id": run["id"], "status": run["status"], "conversation_id": run["conversation_id"], "revision": run["revision"], "parent_run_id": run_id}


def _generate_view(stored: dict[str, Any]) -> dict[str, Any]:
    request = stored.get("request") if isinstance(stored.get("request"), dict) else {}
    response = stored.get("response") if isinstance(stored.get("response"), dict) else {}
    usage = stored.get("usage") if isinstance(stored.get("usage"), dict) else {}
    if not usage:
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "id": stored.get("id") or response.get("id") or "",
        "status": stored.get("status") or response.get("status") or "failed",
        "engine": stored.get("engine") or response.get("engine") or "",
        "provider_id": response.get("provider_id") or request.get("provider_id") or "",
        "model": response.get("model") or request.get("model") or "",
        "reasoning_effort": response.get("reasoning_effort") or request.get("reasoning_effort") or "none",
        "variants": response.get("variants") or [],
        "error": stored.get("error") or response.get("error"),
        "selected_skill_ids": response.get("selected_skill_ids")
            or (request.get("current_document") or {}).get("_explicit_skill_ids")
            or [],
        "skill_diagnostics": response.get("skill_diagnostics") or discovery_diagnostics(),
        "variant_diagnostics": response.get("variant_diagnostics") or [],
        "tool_trace": response.get("tool_trace") or [],
        "usage": {
            "latency_ms": usage.get("latency_ms", stored.get("latency_ms") or response.get("latency_ms")),
            "input_tokens": usage.get("input_tokens", response.get("input_tokens")),
            "output_tokens": usage.get("output_tokens", response.get("output_tokens")),
        },
        "conversation_id": stored.get("conversation_id") or response.get("conversation_id") or "",
        "parent_run_id": stored.get("parent_run_id") or response.get("parent_run_id") or "",
        "revision": stored.get("revision") or response.get("revision") or 1,
        "mode": stored.get("mode") or response.get("mode") or "create",
    }


async def _wait_run_terminal(run_id: str, owner: str) -> dict[str, Any]:
    from .worker import execute_run
    deadline = time.perf_counter() + AGENT_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        row = get_run(run_id)
        status = row.get("status")
        if status in TERMINAL:
            return row
        if status == "queued":
            claimed = claim_run(run_id, owner, lease_seconds=AGENT_TIMEOUT_SECONDS)
            if claimed:
                await execute_run(claimed, owner=owner)
                return get_run(run_id)
        await asyncio.sleep(0.25)
    return get_run(run_id)


@app.post("/api/generate")
async def generate(body: GenerateIn) -> dict[str, Any]:
    from .worker import execute_run
    body, original_intent, conversation_id, revision = _prepare_run_body(body)
    run_id = str(uuid.uuid4())
    if not body.conversation_id:
        conversation_id = run_id
        with connect() as db:
            latest = db.execute("SELECT COALESCE(MAX(revision),0) AS revision FROM agent_runs WHERE conversation_id=?", (conversation_id,)).fetchone()["revision"]
        revision = int(latest) + 1
    owner = f"sync:{owner_id()}"
    try:
        run = create_run(
            run_id=run_id,
            request=body.model_dump(),
            intent=original_intent,
            conversation_id=conversation_id,
            parent_run_id=body.parent_run_id,
            revision=revision,
            mode=body.mode,
            idempotency_key=body.idempotency_key.strip()[:200],
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    claimed = claim_run(run["id"], owner, lease_seconds=AGENT_TIMEOUT_SECONDS)
    if claimed:
        await execute_run(claimed, owner=owner)
    else:
        stored = await _wait_run_terminal(run["id"], owner)
        if stored.get("status") not in TERMINAL:
            raise HTTPException(504, {"code": "run_wait_timeout", "run_id": run["id"], "status": stored.get("status"), "stage": stored.get("stage")})
    return _generate_view(get_run(run["id"]))


@app.get("/api/agent-runs")
def agent_runs(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    items = []
    for row in rows:
        item = {**row_json(row), "request": _json(row["request_json"], {}), "response": _json(row["response_json"], {}), "error": _json(row["error_json"], {})}
        item["tool_trace"] = item["response"].get("tool_trace", []) if isinstance(item["response"], dict) else []
        items.append(item)
    return {"items": items}


@app.get("/api/agent-runs/{run_id}/trace")
def agent_run_trace(run_id: str) -> dict[str, Any]:
    with connect() as db:
        run = db.execute("SELECT id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "agent run not found")
        rows = db.execute("SELECT * FROM agent_events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
    return {"run_id": run_id, "items": [{**row_json(row), "stage": row["step_id"] or "", "arguments": _json(row["arguments_json"], {}), "result": _json(row["result_json"], {}), "error": _json(row["error_json"], {})} for row in rows]}


@app.get("/api/conversations")
def conversations(q: str = "", limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    return list_conversations(q=q, limit=limit, offset=offset)


@app.get("/api/conversations/{conversation_id}")
def conversation_detail(conversation_id: str) -> dict[str, Any]:
    item = get_conversation(conversation_id)
    if not item:
        raise HTTPException(404, "conversation not found")
    return item


@app.get("/api/conversations/{conversation_id}/runs")
def conversation_runs(conversation_id: str) -> dict[str, Any]:
    payload = list_conversation_runs(conversation_id)
    if not payload:
        raise HTTPException(404, "conversation not found")
    return payload


@app.patch("/api/conversations/{conversation_id}")
def conversation_patch(conversation_id: str, body: ConversationPatchIn) -> dict[str, Any]:
    if body.title is None and body.pinned is None:
        raise HTTPException(400, "title or pinned is required")
    item = patch_conversation(conversation_id, title=body.title, pinned=body.pinned)
    if not item:
        raise HTTPException(404, "conversation not found")
    return item


@app.delete("/api/conversations/{conversation_id}")
def conversation_delete(conversation_id: str) -> dict[str, bool]:
    if not delete_conversation(conversation_id):
        raise HTTPException(404, "conversation not found")
    return {"deleted": True}


@app.get("/api/workspace")
def workspace(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    return {
        "status": status(),
        "runtime": _runtime_settings(),
        "providers": providers()["items"],
        "skills": skills()["items"],
        "recent_runs": agent_runs(limit)["items"],
        "conversations": list_conversations(limit=limit, offset=0),
    }


@app.get("/api/documents")
def list_documents() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT * FROM prompt_documents ORDER BY updated_at DESC").fetchall()
    return {"items": [_document_view(row) for row in rows]}


@app.post("/api/documents")
def create_document(body: DocumentIn) -> dict[str, Any]:
    document = canonical_document(body)
    issues = validate_document(document) if document["positive_tokens"] else []
    if issues:
        raise HTTPException(422, {"code": "invalid_document", "issues": issues})
    document_id, stamp = str(uuid.uuid4()), now()
    with connect() as db:
        db.execute(
            "INSERT INTO prompt_documents (id, title, intent, positive_tokens, negative_tokens, protected_tokens, notes, source_run_id, created_at, updated_at, conversation_id, variant_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                document["title"],
                document["intent"],
                json.dumps(document["positive_tokens"], ensure_ascii=False),
                json.dumps(document["negative_tokens"], ensure_ascii=False),
                json.dumps(document["protected_tokens"], ensure_ascii=False),
                document["notes"],
                document["source_run_id"],
                stamp,
                stamp,
                document.get("conversation_id") or "",
                int(document.get("variant_index") or 0),
            ),
        )
        snapshot = _snapshot(document)
        db.execute(
            "INSERT INTO prompt_versions (id, prompt_id, snapshot_json, reason, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), document_id, json.dumps(snapshot, ensure_ascii=False), "create", stamp),
        )
        return {"id": document_id, **_document_view(db.execute("SELECT * FROM prompt_documents WHERE id=?", (document_id,)).fetchone())}


@app.get("/api/documents/{document_id}")
def get_document(document_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM prompt_documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        raise HTTPException(404, "document not found")
    return _document_view(row)


@app.patch("/api/documents/{document_id}")
def update_document(document_id: str, body: DocumentIn) -> dict[str, Any]:
    document = canonical_document(body)
    issues = validate_document(document) if document["positive_tokens"] else []
    if issues:
        raise HTTPException(422, {"code": "invalid_document", "issues": issues})
    with connect() as db:
        return _write_document(db, document_id, document, "edit")


@app.get("/api/documents/{document_id}/versions")
def document_versions(document_id: str) -> dict[str, Any]:
    get_document(document_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM prompt_versions WHERE prompt_id=? ORDER BY created_at DESC", (document_id,)).fetchall()
    return {"items": [{"id": row["id"], "reason": row["reason"], "created_at": row["created_at"], "snapshot": _json(row["snapshot_json"], {})} for row in rows]}


@app.post("/api/documents/{document_id}/restore")
def restore_document(document_id: str, body: dict[str, Any]) -> dict[str, Any]:
    version_id = str(body.get("version_id") or "")
    with connect() as db:
        version = db.execute("SELECT * FROM prompt_versions WHERE id=? AND prompt_id=?", (version_id, document_id)).fetchone()
        if not version:
            raise HTTPException(404, "document version not found")
        document = canonical_document(_json(version["snapshot_json"], {}))
        return _write_document(db, document_id, document, "restore")


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str) -> dict[str, bool]:
    with connect() as db:
        row = db.execute("SELECT id FROM prompt_documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise HTTPException(404, "document not found")
        db.execute("DELETE FROM prompt_documents WHERE id=?", (document_id,))
    return {"deleted": True}


@app.post("/api/documents/lint")
def lint_document(body: dict[str, Any]) -> dict[str, Any]:
    return lint_variant_card(body, enforce_quantity=bool(body.get("enforce_quantity")))


@app.post("/api/documents/{document_id}/validate")
def validate_saved_document(document_id: str) -> dict[str, Any]:
    document = get_document(document_id)
    issues = validate_document(document, enforce_quantity=False)
    return {"valid": not issues, "issues": issues}


@app.post("/api/documents/{document_id}/export")
def export_document(document_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return _export_document(get_document(document_id), str((body or {}).get("format") or "anima"))


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")
