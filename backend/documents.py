from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any

from fastapi import HTTPException

from .db import connect, now, row_json
from .prompt import classify, parse_prompt, serialize_prompt

PROTECTED_RE = re.compile(r"^(?:<lora:[^>]+>|<embed:[^>]+>|BREAK)$", re.I)

# These are workflow-provided effects and must never be emitted as prompt tags.
FORBIDDEN_SECTION_13_6 = frozenset({
    "sunlight", "moonlight", "dim light", "candlelight", "neon light", "neon lights", "streetlights",
    "backlighting", "rim light", "warm lighting", "cool lighting", "golden hour glow", "soft lighting",
    "warm tone", "cool tone", "sepia", "blue tone", "amber tone", "god rays", "light rays",
    "light particles", "volumetric light beams", "tyndall effect", "glowing", "illuminated", "lit",
    "backlit", "spotlight", "flash",
})

def _count_band(document: dict[str, Any]) -> tuple[str, int, int] | None:
    """Infer the template count band only when the prompt declares a multi-person scene."""
    names = {str(item.get("raw_text", "")).strip().casefold() for item in document.get("positive_tokens", [])}
    intent = str(document.get("intent", "")).casefold()
    if {"1girl", "1boy"} <= names or "hetero" in names or "yuri" in names or any(word in intent for word in ("双人", "两人", "前戏", "性交", "体位", "男女", "情侣", "two-person", "foreplay")):
        return "standard", 22, 38
    if names & {"multiple", "multiple girls", "multiple boys", "group sex"} or re.search(r"\b(?:[2-9]|[1-9]\d+)girls?\b|\b(?:[2-9]|[1-9]\d+)boys?\b", " ".join(names)):
        return "complex", 30, 48
    if any(word in intent for word in ("多人", "群交", "剧情主视觉", "特殊主题")):
        return "complex", 30, 48
    return None


def json_value(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def token_dict(token: Any, index: int, side: str) -> dict[str, Any]:
    if hasattr(token, "model_dump"):
        raw = token.model_dump()
    elif isinstance(token, str):
        parsed = parse_prompt(token, f"{side}-{index}")
        raw = parsed[0].json() if len(parsed) == 1 else {"raw_text": token}
    elif isinstance(token, dict):
        raw = dict(token)
    else:
        raise ValueError(f"{side} token {index + 1} must be a string or object")
    text = str(raw.get("raw_text") or "").strip()
    if not text:
        raise ValueError(f"{side} token {index + 1} is empty")
    weight = float(raw.get("weight", 1.0))
    if not math.isfinite(weight) or weight <= 0 or weight > 3:
        raise ValueError(f"{side} token {index + 1} has invalid weight")
    return {
        "id": str(raw.get("id") or f"{side}-{uuid.uuid4().hex[:8]}"),
        "raw_text": text,
        "normalized_tag": str(raw.get("normalized_tag") or text.replace("_", " ")).strip(),
        "category": str(raw.get("category") or classify(text)),
        "weight": weight,
        "source": str(raw.get("source") or "manual"),
        "translation": str(raw.get("translation") or ""),
        "locked": bool(raw.get("locked", False)),
    }


def protected_tokens(tokens: list[dict[str, Any]], explicit: list[str] | None = None) -> list[str]:
    values = [str(item["raw_text"]) for item in tokens if PROTECTED_RE.match(str(item["raw_text"]).strip())]
    values.extend(str(item).strip() for item in (explicit or []) if str(item).strip())
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


def canonical_document(value: Any, *, source_run_id: str = "") -> dict[str, Any]:
    raw = value.model_dump() if hasattr(value, "model_dump") else dict(value)
    positive = [token_dict(item, index, "positive") for index, item in enumerate(raw.get("positive_tokens") or [])]
    negative = [token_dict(item, index, "negative") for index, item in enumerate(raw.get("negative_tokens") or [])]
    return {
        "title": str(raw.get("title") or "Untitled Anima prompt").strip(),
        "intent": str(raw.get("intent") or "").strip(),
        "positive_tokens": positive,
        "negative_tokens": negative,
        "protected_tokens": protected_tokens(positive + negative, raw.get("protected_tokens")),
        "notes": str(raw.get("notes") or "").strip(),
        "source_run_id": str(raw.get("source_run_id") or source_run_id),
    }


def validate_document(document: dict[str, Any], *, enforce_quantity: bool = False) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for side in ("positive_tokens", "negative_tokens"):
        seen: set[str] = set()
        for index, token in enumerate(document.get(side, [])):
            key = str(token.get("raw_text", "")).casefold()
            if key in seen:
                issues.append({"code": "duplicate_token", "side": side, "index": index, "message": f"重复 Token：{token['raw_text']}"})
            seen.add(key)
            weight = token.get("weight", 1)
            if not isinstance(weight, (int, float)) or not math.isfinite(float(weight)) or not 0 < float(weight) <= 3:
                issues.append({"code": "invalid_weight", "side": side, "index": index, "message": f"权重无效：{token.get('raw_text', '')}"})
    positive_names = {str(item.get("raw_text", "")).strip().casefold() for item in document.get("positive_tokens", [])}
    if "solo" in positive_names and (positive_names & {"1girl", "1boy", "2girls", "2boys", "multiple", "multiple girls", "multiple boys", "hetero", "yuri", "group sex"}):
        issues.append({"code": "conflicting_count", "message": "solo 不能与多人或性别数量标签同时出现。"})
    if "1girl" in positive_names and (positive_names & {"2girls", "multiple girls"}):
        issues.append({"code": "conflicting_count", "message": "1girl 不能与 2girls 或 multiple girls 同时出现。"})
    if "1boy" in positive_names and (positive_names & {"2boys", "multiple boys"}):
        issues.append({"code": "conflicting_count", "message": "1boy 不能与 2boys 或 multiple boys 同时出现。"})
    for index, token in enumerate(document.get("positive_tokens", [])):
        raw_text = str(token.get("raw_text", "")).strip().casefold()
        if raw_text in FORBIDDEN_SECTION_13_6:
            issues.append({"code": "forbidden_section_13_6", "side": "positive_tokens", "index": index, "message": f"§13.6 禁令词：{token.get('raw_text', '')}"})
    for index, token in enumerate(document.get("negative_tokens", [])):
        raw_text = str(token.get("raw_text", "")).strip().casefold()
        if raw_text in FORBIDDEN_SECTION_13_6:
            issues.append({"code": "forbidden_section_13_6", "side": "negative_tokens", "index": index, "message": f"§13.6 禁令词：{token.get('raw_text', '')}"})
    if enforce_quantity:
        band = _count_band(document)
        if band:
            label, minimum, maximum = band
            count = len(document.get("positive_tokens", []))
            if count < minimum or count > maximum:
                issues.append({"code": "quantity_out_of_range", "band": label, "minimum": minimum, "maximum": maximum, "actual": count, "message": f"{label} 场景正面 Token 数量应为 {minimum}-{maximum}，当前为 {count}。"})
    if not document.get("positive_tokens"):
        issues.append({"code": "empty_positive", "message": "至少需要一个正面 Token。"})
    return issues


def document_view(row: Any) -> dict[str, Any]:
    item = row_json(row)
    item["positive_tokens"] = json_value(item.pop("positive_tokens", "[]"), [])
    item["negative_tokens"] = json_value(item.pop("negative_tokens", "[]"), [])
    item["protected_tokens"] = json_value(item.pop("protected_tokens", "[]"), [])
    return item


def snapshot(document: dict[str, Any]) -> dict[str, Any]:
    return {key: document.get(key) for key in ("title", "intent", "positive_tokens", "negative_tokens", "protected_tokens", "notes", "source_run_id")}


def write_document(db: Any, document_id: str, document: dict[str, Any], reason: str) -> dict[str, Any]:
    existing = db.execute("SELECT * FROM prompt_documents WHERE id=?", (document_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "document not found")
    db.execute("INSERT INTO prompt_versions(id,prompt_id,snapshot_json,reason,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), document_id, json.dumps(snapshot(document_view(existing)), ensure_ascii=False), reason, now()))
    stamp = now()
    db.execute("UPDATE prompt_documents SET title=?,intent=?,positive_tokens=?,negative_tokens=?,protected_tokens=?,notes=?,source_run_id=?,updated_at=? WHERE id=?", (document["title"], document["intent"], json.dumps(document["positive_tokens"], ensure_ascii=False), json.dumps(document["negative_tokens"], ensure_ascii=False), json.dumps(document["protected_tokens"], ensure_ascii=False), document["notes"], document["source_run_id"], stamp, document_id))
    return document_view(db.execute("SELECT * FROM prompt_documents WHERE id=?", (document_id,)).fetchone())


def export_document(document: dict[str, Any], format: str = "anima") -> dict[str, Any]:
    positive = serialize_prompt(document["positive_tokens"])
    negative = serialize_prompt(document["negative_tokens"])
    if format == "json":
        return {"format": "anima-json", "document": document, "positive": positive, "negative": negative}
    return {"format": "anima", "title": document["title"], "intent": document["intent"], "positive": positive, "negative": negative}
