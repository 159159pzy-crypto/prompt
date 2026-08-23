from __future__ import annotations

import json
import os
import re
import uuid
import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import CATEGORIES, SETTING_TREE, connect, init_db, now, row_json
from .prompt import normalize_prompt_text, parse_prompt, protected_text, restore_protected, serialize_prompt, split_prompt

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="Anima Prompt Workbench", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class PromptIn(BaseModel):
    title: str = "Untitled prompt"
    positive: str = "masterpiece, best quality, 1girl, portrait"
    negative: str = "low quality, bad anatomy"
    payload: dict[str, Any] = Field(default_factory=dict)


class ProviderIn(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(1200, ge=1, le=100000)
    timeout: int = Field(30, ge=1, le=300)
    enabled: bool = True


class FavoriteIn(BaseModel):
    kind: str = "prompt"
    title: str
    body: str


class TagIn(BaseModel):
    tag: str
    translation: str = ""
    category: str = "Custom"


class AgentIn(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    persona: str = ""
    output_schema: dict[str, Any] = Field(default_factory=dict)
    provider_id: str = ""
    model: str = ""
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(1600, ge=1, le=100000)
    context_policy: str = "conversation"
    enabled: bool = True


class AgentRunIn(BaseModel):
    agent_id: str = "anima-creator"
    message: str
    conversation_id: str = ""
    current_prompt_id: str = ""
    max_variants: int = Field(3, ge=1, le=8)


class TranslationConfigIn(BaseModel):
    primary_engine: str = "google"
    fallback_engines: list[str] = Field(default_factory=lambda: ["libretranslate", "argos", "agent"])
    google_endpoint: str = "https://translation.googleapis.com/language/translate/v2"
    google_api_key: str = ""
    source_language: str = "auto"
    target_language: str = "zh-CN"
    timeout: int = Field(20, ge=1, le=180)
    cache_enabled: bool = True
    glossary_id: str = ""


class SourceIn(BaseModel):
    name: str
    kind: str = "custom"
    location: str = ""
    version: str = "local"
    enabled: bool = True
    priority: int = 100


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/status")
def status() -> dict:
    with connect() as db:
        tag_count = db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        agents = db.execute("SELECT COUNT(*) FROM agent_profiles WHERE enabled=1").fetchone()[0]
    return {"ok": True, "name": app.title, "version": app.version, "tag_count": tag_count, "enabled_agents": agents}


def _safe_setting_payload(row: Any) -> dict:
    if not row:
        return {}
    return json.loads(row["payload"])


@app.get("/api/settings/tree")
def settings_tree() -> dict:
    return {"items": [{"id": key, "label": value["label"], "groups": [{"id": gid, "label": glabel} for gid, glabel in value["groups"].items()]} for key, value in SETTING_TREE.items()]}


@app.get("/api/settings/{section}/{group}")
def get_setting(section: str, group: str) -> dict:
    if section not in SETTING_TREE or group not in SETTING_TREE[section]["groups"]:
        raise HTTPException(404, "setting group not found")
    with connect() as db:
        row = db.execute("SELECT payload,updated_at FROM settings WHERE section=? AND group_name=?", (section, group)).fetchone()
        if section == "translation" and group in {"engine", "google", "fallback", "glossary"}:
            translation = db.execute("SELECT * FROM translation_config WHERE id=1").fetchone()
            if translation:
                payload = row_json(translation); payload["google_api_key"] = bool(payload.get("google_api_key")); return {"section": section, "group": group, "payload": payload, "updated_at": now()}
    return {"section": section, "group": group, "payload": _safe_setting_payload(row), "updated_at": row["updated_at"] if row else None}


@app.put("/api/settings/{section}/{group}")
def put_setting(section: str, group: str, payload: dict[str, Any]) -> dict:
    if section not in SETTING_TREE or group not in SETTING_TREE[section]["groups"]:
        raise HTTPException(404, "setting group not found")
    stamp = now()
    with connect() as db:
        if section == "translation":
            current = db.execute("SELECT * FROM translation_config WHERE id=1").fetchone()
            values = {key: current[key] for key in current.keys()} if current else {}
            values.update(payload)
            if "google_api_key" not in payload:
                values["google_api_key"] = current["google_api_key"] if current else ""
            db.execute("INSERT OR REPLACE INTO translation_config VALUES(1,?,?,?,?,?,?,?,?,?)", (values.get("primary_engine", "google"), json.dumps(values.get("fallback_engines", ["libretranslate", "argos", "agent"])), values.get("google_endpoint", "https://translation.googleapis.com/language/translate/v2"), values.get("google_api_key", ""), values.get("source_language", "auto"), values.get("target_language", "zh-CN"), int(values.get("timeout", 20)), int(values.get("cache_enabled", True)), values.get("glossary_id", "")))
        else:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?,?)", (section, group, json.dumps(payload, ensure_ascii=False), stamp))
    return get_setting(section, group)


@app.get("/api/agents")
def list_agents() -> dict:
    with connect() as db:
        rows = db.execute("SELECT id,name,description,system_prompt,persona,output_schema,provider_id,model,temperature,max_tokens,context_policy,enabled,created_at,updated_at FROM agent_profiles ORDER BY created_at").fetchall()
    items = []
    for row in rows:
        item = row_json(row); item["output_schema"] = json.loads(item["output_schema"]); items.append(item)
    return {"items": items}


@app.post("/api/agents")
def create_agent(body: AgentIn) -> dict:
    agent_id = "agent-" + uuid.uuid4().hex[:12]; stamp = now()
    with connect() as db:
        db.execute("INSERT INTO agent_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (agent_id, body.name, body.description, body.system_prompt, body.persona, json.dumps(body.output_schema, ensure_ascii=False), body.provider_id, body.model, body.temperature, body.max_tokens, body.context_policy, int(body.enabled), stamp, stamp))
    return next(item for item in list_agents()["items"] if item["id"] == agent_id)


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentIn) -> dict:
    with connect() as db:
        if not db.execute("SELECT 1 FROM agent_profiles WHERE id=?", (agent_id,)).fetchone(): raise HTTPException(404, "agent not found")
        db.execute("UPDATE agent_profiles SET name=?,description=?,system_prompt=?,persona=?,output_schema=?,provider_id=?,model=?,temperature=?,max_tokens=?,context_policy=?,enabled=?,updated_at=? WHERE id=?", (body.name, body.description, body.system_prompt, body.persona, json.dumps(body.output_schema, ensure_ascii=False), body.provider_id, body.model, body.temperature, body.max_tokens, body.context_policy, int(body.enabled), now(), agent_id))
    return next(item for item in list_agents()["items"] if item["id"] == agent_id)


def _fallback_variants(message: str, count: int) -> list[dict]:
    words = [part.strip() for part in re.split(r"[，,。；;\n]", message) if part.strip()]
    english = ["masterpiece", "best quality"]
    mapping = {"女孩": "1girl", "男孩": "1boy", "长发": "long hair", "蓝天": "blue sky", "校服": "school uniform", "站": "standing", "坐": "sitting", "微笑": "smile", "肖像": "portrait", "户外": "outdoors", "柔和光线": "soft lighting"}
    for word in words:
        for key, tag in mapping.items():
            if key in word and tag not in english: english.append(tag)
    variants = []
    for index in range(count):
        variants.append({"title": f"Anima variant {index + 1}", "natural_language": message, "positive": english + (["cinematic composition"] if index else []), "negative": ["low quality", "bad anatomy"], "recognized_tags": english[2:], "unknown_terms": [], "notes": ["供应商不可用，已使用本地候选回退"]})
    return variants


async def _call_agent(agent: dict, message: str, max_variants: int) -> tuple[list[dict], str]:
    provider = None
    with connect() as db:
        if agent.get("provider_id"):
            provider = db.execute("SELECT * FROM providers WHERE id=?", (agent["provider_id"],)).fetchone()
        if not provider:
            provider = db.execute("SELECT * FROM providers WHERE enabled=1 ORDER BY rowid LIMIT 1").fetchone()
    if not provider or not provider["api_key"]:
        return _fallback_variants(message, max_variants), "local-fallback"
    schema = agent.get("output_schema") or {"type": "object", "required": ["variants"]}
    request = {"model": agent.get("model") or provider["model"], "temperature": agent.get("temperature", provider["temperature"]), "max_tokens": agent.get("max_tokens", provider["max_tokens"]), "messages": [{"role": "system", "content": f"{agent['system_prompt']}\nPersona: {agent['persona']}\nOutput JSON schema: {json.dumps(schema, ensure_ascii=False)}"}, {"role": "user", "content": message}]}
    try:
        async with httpx.AsyncClient(timeout=provider["timeout"]) as client:
            response = await client.post(provider["base_url"].rstrip("/") + "/chat/completions", json=request, headers={"Authorization": f"Bearer {provider['api_key']}"})
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I).strip()
            parsed = json.loads(content)
            variants = parsed.get("variants", []) if isinstance(parsed, dict) else []
            return variants[:max_variants], "openai-compatible"
    except Exception:
        return _fallback_variants(message, max_variants), "local-fallback"


@app.post("/api/agent-runs")
async def create_agent_run(body: AgentRunIn) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM agent_profiles WHERE id=? AND enabled=1", (body.agent_id,)).fetchone()
    if not row: raise HTTPException(404, "agent not found")
    agent = row_json(row); agent["output_schema"] = json.loads(agent["output_schema"])
    variants, engine = await _call_agent(agent, body.message, body.max_variants)
    run_id = str(uuid.uuid4()); response = {"variants": variants, "engine": engine, "agent_id": body.agent_id}
    with connect() as db:
        db.execute("INSERT INTO agent_runs VALUES(?,?,?,?,?,?)", (run_id, body.agent_id, body.message, json.dumps(response, ensure_ascii=False), "completed", now()))
    return {"id": run_id, **response}


@app.get("/api/agent-runs")
def list_agent_runs() -> dict:
    with connect() as db: rows = db.execute("SELECT id,agent_id,message,status,created_at FROM agent_runs ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.get("/api/catalog/categories")
def categories() -> dict:
    return {"categories": CATEGORIES}


@app.post("/api/prompts/normalize")
def normalize_prompts(body: dict[str, Any]) -> dict:
    positive = str(body.get("positive", "")); negative = str(body.get("negative", ""))
    protected = body.get("protected_lora", []) or []
    normalized_positive, positive_changes = normalize_prompt_text(positive, protected_lora=protected)
    normalized_negative, negative_changes = normalize_prompt_text(negative, protected_lora=protected)
    return {"positive": normalized_positive, "negative": normalized_negative, "changes": [{"field": "positive", **change} for change in positive_changes] + [{"field": "negative", **change} for change in negative_changes], "changed": normalized_positive != positive or normalized_negative != negative}


@app.post("/api/prompts/{prompt_id}/normalize")
def normalize_saved_prompt(prompt_id: str) -> dict:
    prompt = get_prompt(prompt_id)
    normalized = normalize_prompts({"positive": prompt["positive"], "negative": prompt["negative"]})
    with connect() as db:
        event_id = str(uuid.uuid4())
        db.execute("INSERT INTO normalization_events VALUES(?,?,?,?,?)", (event_id, prompt_id, json.dumps({"positive": prompt["positive"], "negative": prompt["negative"]}, ensure_ascii=False), json.dumps({"positive": normalized["positive"], "negative": normalized["negative"]}, ensure_ascii=False), json.dumps(normalized["changes"], ensure_ascii=False), now()))
        if normalized["changed"]:
            db.execute("UPDATE prompts SET positive=?,negative=?,updated_at=? WHERE id=?", (normalized["positive"], normalized["negative"], now(), prompt_id))
    return {"prompt": get_prompt(prompt_id), "changes": normalized["changes"], "event_id": event_id}


@app.get("/api/prompts/{prompt_id}/normalization-history")
def normalization_history(prompt_id: str) -> dict:
    with connect() as db: rows = db.execute("SELECT * FROM normalization_events WHERE prompt_id=? ORDER BY created_at DESC", (prompt_id,)).fetchall()
    items = []
    for row in rows:
        item = row_json(row); item["before"] = json.loads(item.pop("before_json")); item["after"] = json.loads(item.pop("after_json")); item["changes"] = json.loads(item.pop("changes_json")); items.append(item)
    return {"items": items}


@app.post("/api/prompts/{prompt_id}/restore-version")
def restore_prompt_version(prompt_id: str, body: dict[str, Any]) -> dict:
    prompt = get_prompt(prompt_id); before = body.get("before") or {}
    if not isinstance(before, dict) or "positive" not in before or "negative" not in before: raise HTTPException(400, "before must contain positive and negative")
    with connect() as db:
        db.execute("UPDATE prompts SET positive=?,negative=?,updated_at=? WHERE id=?", (str(before["positive"]), str(before["negative"]), now(), prompt_id))
    return get_prompt(prompt_id)


@app.get("/api/catalog/sources")
def catalog_sources() -> dict:
    with connect() as db: rows = db.execute("SELECT * FROM catalog_sources ORDER BY priority, name").fetchall()
    return {"items": [row_json(row) for row in rows]}


def _read_js_array(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8")
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        return []
    payload = json.loads(content[start : end + 1])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _real_source_items(source: Any) -> list[TagIn]:
    """Load the installed Anima Tools JS catalogs into the normalized tag table."""
    root = Path(source["location"])
    js_root = root / "js" if (root / "js").is_dir() else root
    if source["kind"] == "anima-tools" and not (js_root / "character_data.js").exists():
        return []
    items: list[TagIn] = []
    files = [("character_data.js", "Character"), ("clothing_data.js", "Clothing"), ("pose_data.js", "Pose / Action"), ("background_data.js", "Background / Scene")]
    for filename, category in files:
        path = js_root / filename
        if not path.exists():
            continue
        try:
            records = _read_js_array(path)
        except (OSError, ValueError):
            continue
        for item in records:
            name = str(item.get("name") or item.get("name_zh") or "").strip()
            if not name:
                continue
            if category == "Character":
                parts = [name, str(item.get("gender") or ""), str(item.get("hair") or ""), str(item.get("eye") or "")]
                translation = " / ".join(part for part in [str(item.get("name_zh") or ""), str(item.get("copyright") or "")] if part)
            else:
                raw_tags = item.get("tags") or item.get("prompt") or ""
                parts = [part.strip() for part in str(raw_tags).split(",") if part.strip()]
                translation = str(item.get("name_zh") or item.get("tags_zh") or "")
            for tag in dict.fromkeys(part for part in parts if part):
                items.append(TagIn(tag=tag, translation=translation, category=category))
    return items


@app.post("/api/catalog/sources")
def create_catalog_source(body: SourceIn) -> dict:
    source_id = "source-" + uuid.uuid4().hex[:12]
    with connect() as db: db.execute("INSERT INTO catalog_sources VALUES(?,?,?,?,?,?,?,?,?)", (source_id, body.name, body.kind, body.location, body.version, int(body.enabled), body.priority, 0, "", "not_synced"))
    return next(item for item in catalog_sources()["items"] if item["id"] == source_id)


@app.post("/api/catalog/sources/import")
def import_catalog_source(source_id: str, items: list[TagIn]) -> dict:
    with connect() as db:
        source = db.execute("SELECT * FROM catalog_sources WHERE id=?", (source_id,)).fetchone()
        if not source: raise HTTPException(404, "source not found")
        for item in items:
            tag = item.tag.strip()
            db.execute("INSERT INTO tags(id,tag,translation,category,source) VALUES(?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET translation=CASE WHEN excluded.translation!='' THEN excluded.translation ELSE tags.translation END, category=excluded.category, source=excluded.source", (str(uuid.uuid4()), tag, item.translation, item.category, source["name"]))
        db.execute("UPDATE catalog_sources SET item_count=?,last_sync=?,status='ready' WHERE id=?", (len(items), now(), source_id))
    return {"source_id": source_id, "imported": len(items)}


@app.post("/api/catalog/sources/sync")
def sync_catalog_source(source_id: str) -> dict:
    with connect() as db: source = db.execute("SELECT * FROM catalog_sources WHERE id=?", (source_id,)).fetchone()
    if not source: raise HTTPException(404, "source not found")
    location = source["location"]
    if not Path(location).exists():
        return {"ok": False, "source_id": source_id, "status": "unavailable", "message": f"数据源路径不存在：{location}"}
    items = _real_source_items(source)
    if items:
        import_catalog_source(source_id, items)
        return {"ok": True, "source_id": source_id, "status": "ready", "imported": len(items), "message": f"已导入真实 Anima Tools 数据：{len(items)} 个 tag"}
    with connect() as db: db.execute("UPDATE catalog_sources SET last_sync=?,status='ready' WHERE id=?", (now(), source_id))
    return {"ok": True, "source_id": source_id, "status": "ready", "message": "路径已确认，但未找到可解析的目录文件"}


@app.get("/api/translation/config")
def translation_config() -> dict:
    with connect() as db: row = db.execute("SELECT * FROM translation_config WHERE id=1").fetchone()
    payload = row_json(row); payload["fallback_engines"] = json.loads(payload["fallback_engines"]); payload["has_google_api_key"] = bool(payload.pop("google_api_key", "")); return payload


@app.put("/api/translation/config")
def put_translation_config(body: TranslationConfigIn) -> dict:
    with connect() as db:
        previous = db.execute("SELECT google_api_key FROM translation_config WHERE id=1").fetchone()
        api_key = body.google_api_key or (previous["google_api_key"] if previous else "")
        db.execute("INSERT OR REPLACE INTO translation_config VALUES(1,?,?,?,?,?,?,?,?,?)", (body.primary_engine, json.dumps(body.fallback_engines), body.google_endpoint, api_key, body.source_language, body.target_language, body.timeout, int(body.cache_enabled), body.glossary_id))
    return translation_config()


@app.post("/api/translation/test")
async def test_translation() -> dict:
    with connect() as db: row = db.execute("SELECT * FROM translation_config WHERE id=1").fetchone()
    if not row or not row["google_api_key"]: return {"ok": False, "engine": "google", "error": "δ���� Google Cloud API key"}
    try:
        async with httpx.AsyncClient(timeout=row["timeout"]) as client:
            response = await client.post(row["google_endpoint"], params={"key": row["google_api_key"]}, json={"q": "Anima prompt", "target": row["target_language"], "format": "text"})
            response.raise_for_status()
        return {"ok": True, "engine": "google", "status": response.status_code}
    except Exception as exc: return {"ok": False, "engine": "google", "error": str(exc)}


@app.get("/api/catalog/tags")
def tags(q: str = "", category: str = "", limit: int = Query(100, ge=1, le=500)) -> dict:
    qn = q.replace("_", " ").strip().lower()
    clauses, values = [], []
    if qn:
        clauses.append("(lower(replace(tag, '_', ' ')) LIKE ? OR lower(translation) LIKE ?)")
        values += [f"%{qn}%", f"%{qn}%"]
    if category and category != "All":
        clauses.append("category = ?"); values.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as db:
        rows = db.execute(f"SELECT * FROM tags{where} ORDER BY favorite DESC, usage_count DESC, tag LIMIT ?", [*values, limit]).fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.post("/api/catalog/recognize")
def recognize_tags(body: dict[str, Any]) -> dict:
    text = str(body.get("text", ""))
    parts = split_prompt(text)
    items = []
    with connect() as db:
        for part in parts:
            normalized = part.replace("_", " ").strip().lower()
            row = db.execute("SELECT * FROM tags WHERE lower(replace(tag, '_', ' '))=? OR lower(translation)=? LIMIT 1", (normalized, normalized)).fetchone()
            protected = normalized == "break" or normalized.startswith("<lora:") or normalized.startswith("<embed:")
            items.append({"raw": part, "recognized": bool(row) or protected, "tag": row["tag"] if row else part, "category": row["category"] if row else ("Composition / Camera" if normalized == "break" else "LoRA / Embedding" if protected else "Unknown"), "source": row["source"] if row else ("protected" if protected else "unresolved"), "translation": row["translation"] if row else ""})
    return {"items": items, "unknown": [item["raw"] for item in items if not item["recognized"]]}


@app.post("/api/catalog/tags")
def add_tag(body: TagIn) -> dict:
    tag = body.tag.strip()
    if not tag:
        raise HTTPException(400, "tag is required")
    with connect() as db:
        db.execute("INSERT INTO tags(id,tag,translation,category,source) VALUES(?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET translation=excluded.translation, category=excluded.category", (str(uuid.uuid4()), tag, body.translation, body.category, "custom"))
        row = db.execute("SELECT * FROM tags WHERE tag=?", (tag,)).fetchone()
    return row_json(row)


@app.post("/api/catalog/import")
def import_tags(items: list[TagIn]) -> dict:
    with connect() as db:
        for item in items:
            db.execute("INSERT INTO tags(id,tag,translation,category,source) VALUES(?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET translation=excluded.translation, category=excluded.category", (str(uuid.uuid4()), item.tag.strip(), item.translation, item.category, "import"))
    return {"imported": len(items)}


@app.get("/api/prompts")
def list_prompts() -> dict:
    with connect() as db:
        rows = db.execute("SELECT id,title,positive,negative,created_at,updated_at FROM prompts ORDER BY updated_at DESC").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.post("/api/prompts")
def create_prompt(body: PromptIn) -> dict:
    prompt_id = str(uuid.uuid4()); timestamp = now()
    payload = body.payload or {"positive": [x.json() for x in parse_prompt(body.positive, "pos")], "negative": [x.json() for x in parse_prompt(body.negative, "neg")]}
    with connect() as db:
        db.execute("INSERT INTO prompts VALUES(?,?,?,?,?,?,?)", (prompt_id, body.title, body.positive, body.negative, json.dumps(payload, ensure_ascii=False), timestamp, timestamp))
    return get_prompt(prompt_id)


@app.get("/api/prompts/{prompt_id}")
def get_prompt(prompt_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if not row: raise HTTPException(404, "prompt not found")
    result = row_json(row); result["payload"] = json.loads(result["payload"]); return result


@app.patch("/api/prompts/{prompt_id}")
def update_prompt(prompt_id: str, body: PromptIn) -> dict:
    timestamp = now(); payload = body.payload or {"positive": [x.json() for x in parse_prompt(body.positive, "pos")], "negative": [x.json() for x in parse_prompt(body.negative, "neg")]}
    with connect() as db:
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone(): raise HTTPException(404, "prompt not found")
        db.execute("UPDATE prompts SET title=?,positive=?,negative=?,payload=?,updated_at=? WHERE id=?", (body.title, body.positive, body.negative, json.dumps(payload, ensure_ascii=False), timestamp, prompt_id))
    return get_prompt(prompt_id)


@app.post("/api/prompts/{prompt_id}/translate")
async def translate_prompt(prompt_id: str, target: str = "zh") -> dict:
    prompt = get_prompt(prompt_id)
    protected, placeholders = protected_text(prompt["positive"])
    translated = protected
    provider = None
    with connect() as db:
        provider_row = db.execute("SELECT * FROM providers WHERE enabled=1 ORDER BY rowid LIMIT 1").fetchone()
    if provider_row and provider_row["api_key"]:
        provider = row_json(provider_row)
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        request = {"model": provider["model"], "temperature": provider["temperature"], "messages": [{"role": "system", "content": "Translate the user text to Chinese. Preserve placeholders exactly and return only the translation."}, {"role": "user", "content": protected}]}
        try:
            async with httpx.AsyncClient(timeout=provider["timeout"]) as client:
                response = await client.post(url, json=request, headers={"Authorization": f"Bearer {provider['api_key']}"})
                response.raise_for_status(); translated = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            return {"ok": False, "source": prompt["positive"], "translation": prompt["positive"], "engine": "none", "error": str(exc)}
    translated = restore_protected(translated, placeholders)
    return {"ok": True, "source": prompt["positive"], "translation": translated, "engine": "openai-compatible" if provider else "local-placeholder", "note": "最终英文 tags 未被替换；翻译仅作辅助显示"}


@app.get("/api/providers")
def providers() -> dict:
    with connect() as db: rows = db.execute("SELECT id,name,base_url,model,temperature,max_tokens,timeout,enabled,CASE WHEN api_key='' THEN 0 ELSE 1 END AS has_api_key FROM providers ORDER BY rowid").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.post("/api/providers")
def save_provider(body: ProviderIn) -> dict:
    provider_id = str(uuid.uuid4())
    with connect() as db: db.execute("INSERT INTO providers VALUES(?,?,?,?,?,?,?,?,?)", (provider_id, body.name, body.base_url, body.model, body.api_key, body.temperature, body.max_tokens, body.timeout, int(body.enabled)))
    return {"id": provider_id, "name": body.name, "base_url": body.base_url, "model": body.model, "has_api_key": bool(body.api_key)}


@app.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict:
    with connect() as db: row = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not row: raise HTTPException(404, "provider not found")
    if not row["api_key"]: return {"ok": False, "error": "未配置 API key"}
    try:
        async with httpx.AsyncClient(timeout=row["timeout"]) as client:
            response = await client.get(row["base_url"].rstrip("/") + "/models", headers={"Authorization": f"Bearer {row['api_key']}"})
            response.raise_for_status()
        return {"ok": True, "status": response.status_code}
    except Exception as exc: return {"ok": False, "error": str(exc)}


@app.get("/api/skills")
def skills() -> dict:
    with connect() as db: rows = db.execute("SELECT * FROM skills ORDER BY sort_order").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.patch("/api/skills/{skill_id}")
def update_skill(skill_id: str, enabled: bool) -> dict:
    with connect() as db: db.execute("UPDATE skills SET enabled=? WHERE id=?", (int(enabled), skill_id)); row = db.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
    if not row: raise HTTPException(404, "skill not found")
    return row_json(row)


@app.get("/api/favorites")
def favorites() -> dict:
    with connect() as db: rows = db.execute("SELECT * FROM favorites ORDER BY created_at DESC").fetchall()
    return {"items": [row_json(row) for row in rows]}


@app.post("/api/favorites")
def add_favorite(body: FavoriteIn) -> dict:
    item = (str(uuid.uuid4()), body.kind, body.title, body.body, now())
    with connect() as db: db.execute("INSERT INTO favorites VALUES(?,?,?,?,?)", item)
    return {"id": item[0], "kind": item[1], "title": item[2], "body": item[3], "created_at": item[4]}


@app.post("/api/prompts/{prompt_id}/export")
def export_prompt(prompt_id: str, format: str = "anima") -> dict:
    prompt = get_prompt(prompt_id); positive, negative = prompt["positive"], prompt["negative"]
    if format == "json": return {"title": prompt["title"], "positive": positive, "negative": negative, "payload": prompt["payload"]}
    if format == "markdown": return {"content": f"# {prompt['title']}\n\n## Positive\n\n`{positive}`\n\n## Negative\n\n`{negative}`"}
    return {"positive": positive, "negative": negative, "format": "anima-compatible"}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse: return FileResponse(ROOT / "static" / "index.html")
