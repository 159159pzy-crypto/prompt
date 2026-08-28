from __future__ import annotations

import json
import re
import time
import asyncio
from typing import Any, Callable

import httpx
from json_repair import repair_json

from .documents import PROTECTED_RE, canonical_document, validate_document
from .prompt import split_prompt
from . import skill_runtime
from .skills import dimension_hints, instructions as skill_instructions, selected_ids as selected_skill_ids
from .persona import STUDIO_PERSONA

_CN_NUMBERS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
VARIANT_OVERLAP_LIMIT = 0.5


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CN_NUMBERS:
        return _CN_NUMBERS[value]
    if value.endswith("十") and value[:-1] in _CN_NUMBERS:
        return _CN_NUMBERS[value[:-1]] * 10
    if value.startswith("十"):
        return 10 + (_CN_NUMBERS.get(value[1:]) or 0)
    if "十" in value:
        left, right = value.split("十", 1)
        if left in _CN_NUMBERS and (not right or right in _CN_NUMBERS):
            return _CN_NUMBERS[left] * 10 + (_CN_NUMBERS.get(right) or 0)
    return None


def parse_generation_request(intent: str, fallback_count: int = 1) -> dict[str, Any]:
    """Extract explicit variant count and semantic dimensions from user text."""
    text = str(intent or "").strip()
    explicit_count = False
    count: int | None = None
    patterns = (
        r"(?<![0-9])([0-9]{1,5}|[零一二两三四五六七八九十百]+)\s*(?:组|套|份|种|条|prompts?|sets?|variants?)",
        r"(?:generate|make|give me|create)\s+([0-9]{1,5})\s+(?:prompts?|sets?|variants?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            parsed = _chinese_number(match.group(1))
            if parsed and parsed > 0:
                count = parsed
                explicit_count = True
                break
    hints = dimension_hints()
    dimensions = [
        skill_id
        for skill_id, triggers in hints.items()
        if any(skill_runtime._trigger_match(trigger, text) for trigger in triggers)
    ]
    lowered = text.casefold()
    dedupe_required = bool((count or fallback_count) > 1 or any(word in lowered for word in ("不重复", "不同", "变体", "variation", "distinct", "unique")))
    return {
        "requested_count": count or max(1, int(fallback_count or 1)),
        "explicit_count": explicit_count,
        "variation_dimensions": dimensions,
        "dedupe_required": dedupe_required,
    }


async def _agent_parse_request(intent: str, provider: Any, secret: str, fallback_count: int, model: str = "") -> dict[str, Any] | None:
    """Ask the provider to resolve vague multi-variant language when local rules cannot."""
    if not provider or not secret:
        return None
    prompt = (
        "Extract generation intent as JSON only. Return exactly these fields: "
        "requested_count (positive integer), explicit_count (boolean), "
        "variation_dimensions (array of skill ids), dedupe_required (boolean). "
        "If no explicit count is present, use 1. Count people (两个人/五个女孩) as one image, "
        "not as variant count. Valid dimensions: clothing-library, pose-library, "
        "camera-scene-library, appearance-library, mood-library, expression-library, special-themes.\nUser: " + intent
    )
    request = {
        # Keep the helper request on the same explicitly selected route as the
        # main generation request. The provider default may be stale.
        "model": model or provider["model"],
        "temperature": 0,
        "messages": [{"role": "system", "content": "Return JSON only."}, {"role": "user", "content": prompt}],
        "max_tokens": 256,
    }
    try:
        async with httpx.AsyncClient(timeout=min(float(provider["timeout"] if "timeout" in provider.keys() else 120), 30.0)) as client:
            response = await client.post(provider["base_url"].rstrip("/") + "/chat/completions", json=request, headers={"Authorization": f"Bearer {secret}"})
            response.raise_for_status()
            payload = response.json()
            content = _message_content(payload.get("choices", [{}])[0].get("message", {}))
            parsed = _parse_model_json(content)
            count = int(parsed.get("requested_count") or fallback_count or 1)
            if count < 1:
                return None
            allowed = dimension_hints()
            dimensions = [str(item) for item in parsed.get("variation_dimensions", []) if str(item) in allowed]
            return {"requested_count": count, "explicit_count": bool(parsed.get("explicit_count")), "variation_dimensions": dimensions, "dedupe_required": bool(parsed.get("dedupe_required", count > 1))}
    except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError):
        return None


def _cross_variant_diagnostics(variants: list[dict[str, Any]], dimensions: list[str]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    token_sets = [set(str(token.get("raw_text", "")).casefold() for token in item.get("positive_tokens", []) if isinstance(token, dict)) for item in variants]
    for left in range(len(token_sets)):
        for right in range(left + 1, len(token_sets)):
            shared = token_sets[left] & token_sets[right]
            denominator = max(1, len(token_sets[left] | token_sets[right]))
            overlap = len(shared) / denominator
            if overlap >= VARIANT_OVERLAP_LIMIT:
                diagnostics.append({"code": "variant_too_similar", "variants": [left + 1, right + 1], "overlap": round(overlap, 3), "dimensions": dimensions})
    return diagnostics


def error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message[:500]}


DEFAULT_SYSTEM_PROMPT = """You create production-ready Anima image prompts. Return JSON only.
The canonical output is structured tokens, never a prose-only prompt.
Each token may be either a string such as {\"raw_text\":\"1girl\",\"weight\":1} or an object with raw_text and weight.
Prefer the object form. Return positive_tokens only. Do not return negative_tokens or a negative prompt.
Before returning any JSON, you MUST call validate_prompt with enforce_quantity=true. Only after it passes, return the final JSON."""

TRANSLATION_RULE = (
    "include_chinese is TRUE for this request. You MUST return positive_translations: "
    "item i is the Simplified Chinese translation of positive token i. "
    "NEVER copy the English tag itself as its translation (e.g. '1girl' must be '一个女孩', "
    "'cowgirl position' must be '骑乘位', 'solo' must be '单人'). Each array MUST have exactly "
    "the same length and order as positive_tokens; never summarize, merge, "
    "reorder, or omit tokens. Only LoRA, Embedding, BREAK and trigger-word items stay verbatim."
)


class ModelResponseError(ValueError):
    """A model response violated the generation contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ValidationFailed(ValueError):
    """Deterministic variant validation failed with structured issues."""

    def __init__(self, issues: list[dict[str, Any]], message: str = "") -> None:
        self.issues = list(issues)
        super().__init__(message or "; ".join(str(item.get("message") or "") for item in self.issues))


def _token_list(value: Any, field: str) -> list[Any]:
    """Accept common model shorthand, then canonicalize it into Token objects."""
    if value is None:
        return []
    if isinstance(value, str):
        return split_prompt(value)
    if isinstance(value, list):
        return value
    raise ValueError(f"{field} must be a string or array")


def _translation_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ModelResponseError("token_translation_invalid", f"模型返回的 {field} 必须是非空字符串数组。")
    return [item.strip() for item in value]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(_content_text(part) for part in value).strip()
    if isinstance(value, dict):
        return _content_text(value.get("text") or value.get("content"))
    return ""


def _json_response_candidate(text: str) -> str:
    """Recover a JSON answer that an OpenAI-compatible router put in reasoning."""
    text = text.strip()
    if not text:
        return ""
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
    for candidate in reversed(fenced):
        if '"variants"' in candidate:
            return candidate.strip()
    decoder = json.JSONDecoder()
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("variants"), list):
            return text[start:end]
    if '"variants"' in text:
        start = text.find("{")
        if start >= 0:
            return text[start:]
    return ""


def _message_content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = _content_text(message.get("content"))
    if content:
        return content
    for key in ("reasoning_content", "reasoning", "output_text"):
        candidate = _json_response_candidate(_content_text(message.get(key)))
        if candidate:
            return candidate
    return ""


def _usage_tokens(usage: Any) -> tuple[Any, Any]:
    if not isinstance(usage, dict):
        return None, None
    return (
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
    )


def _parse_model_json(content: str) -> dict[str, Any]:
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = repair_json(content, return_objects=True)
    if not isinstance(parsed, dict):
        raise ModelResponseError(
            "provider_json_invalid",
            "模型返回的 JSON 格式有误，自动修复失败；请重试或切换模型。",
        )
    return parsed


MAX_TOOL_RESULT_CHARS = 12000
MAX_AGENT_ROUNDS = 16
AGENT_TIMEOUT_SECONDS = 300
MAX_TOOL_CALLS = 32


def _clip(value: Any, limit: int = MAX_TOOL_RESULT_CHARS) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_clip(item, limit) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _clip(item, limit) for key, item in list(value.items())[:100]}
    return value


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "list_skills", "description": "List all available read-only repository Skills and their reference sections.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "read_skill", "description": "Read a Skill index, or one reference section when section is set.", "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"}, "section": {"type": "string", "description": "Optional references/<section>.md id"}}, "required": ["skill_id"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "validate_prompt", "description": "Validate a structured Anima prompt document and return issues.", "parameters": {"type": "object", "properties": {"document": {"type": "object"}, "enforce_quantity": {"type": "boolean"}}, "required": ["document"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "normalize_prompt", "description": "Normalize a structured Anima prompt document into canonical Token objects.", "parameters": {"type": "object", "properties": {"document": {"type": "object"}}, "required": ["document"], "additionalProperties": False}}},
    ]


def _tool_list_skills() -> dict[str, Any]:
    items, diagnostics = skill_runtime.catalog({})
    return {"items": _clip(items), "diagnostics": _clip(diagnostics)}


def _tool_read_skill(arguments: dict[str, Any], injected_skill_ids: set[str] | None = None) -> dict[str, Any]:
    skill_id = str(arguments.get("skill_id") or "").strip()
    if not skill_id:
        raise ValueError("skill_id is required")
    discovered, _diagnostics = skill_runtime.discover()
    skill = next((item for item in discovered if item.id == skill_id), None)
    if skill is None:
        raise ValueError(f"unknown skill: {skill_id}")
    sections = [item["id"] for item in skill_runtime.list_sections(skill) if item["available"]]
    item = {"id": skill.id, "name": skill.display_name or skill.name, "description": skill.description, "sections": sections}
    section = str(arguments.get("section") or "").strip()
    if section:
        return {**item, "section": section, "instruction_injected": False, "instruction": _clip(skill_runtime.load_section(skill, section))}
    if injected_skill_ids and skill.id in injected_skill_ids:
        hint = "该 Skill 索引已注入当前 system message。"
        if sections:
            hint += " 词表请用 section 参数读取：" + ", ".join(sections)
        else:
            hint += " 无需重复读取正文。"
        return {**item, "instruction_injected": True, "instruction": hint}
    return {**item, "instruction_injected": False, "instruction": _clip(skill_runtime.load_instructions(skill))}


def _tool_validate_prompt(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_document = arguments.get("document")
    if not isinstance(raw_document, dict):
        raise ValueError("document must be an object")
    document = canonical_document(raw_document)
    issues = validate_document(document, enforce_quantity=bool(arguments.get("enforce_quantity", False)))
    return {"valid": not issues, "issues": _clip(issues), "document": _clip(document)}


def _tool_normalize_prompt(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_document = arguments.get("document")
    if not isinstance(raw_document, dict):
        raise ValueError("document must be an object")
    document = canonical_document(raw_document)
    return {"document": _clip(document), "protected_tokens": document.get("protected_tokens", [])}


def _execute_tool(name: str, arguments: dict[str, Any], injected_skill_ids: set[str] | None = None) -> dict[str, Any]:
    if name == "list_skills":
        return _tool_list_skills()
    if name == "read_skill":
        return _tool_read_skill(arguments, injected_skill_ids)
    if name == "validate_prompt":
        return _tool_validate_prompt(arguments)
    if name == "normalize_prompt":
        return _tool_normalize_prompt(arguments)
    raise ValueError(f"unknown tool: {name}")


def _tool_call_items(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
        return []
    items: list[dict[str, Any]] = []
    for item in message["tool_calls"]:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function.get("arguments", {})
        if isinstance(raw_arguments, str):
            try:
                raw_arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                raw_arguments = {}
        items.append({"id": str(item.get("id") or f"call_{len(items) + 1}"), "name": name, "arguments": raw_arguments if isinstance(raw_arguments, dict) else {}})
    return items


def validate_variant(raw: dict[str, Any], include_chinese: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("variant must be an object")
    document = canonical_document({
        "title": raw.get("title") or "Anima candidate",
        "intent": raw.get("intent") or "",
        "positive_tokens": _token_list(raw.get("positive_tokens", raw.get("positive")), "positive_tokens"),
        "negative_tokens": [],
        "protected_tokens": raw.get("protected_tokens") or [],
        "notes": raw.get("notes") or "",
    })
    normalized = _tool_normalize_prompt({"document": document})["document"]
    document = normalized
    issues = _tool_validate_prompt({"document": document, "enforce_quantity": True})["issues"]
    if issues:
        raise ValidationFailed(issues)
    if include_chinese:
        translations = raw.get("translations") or {}
        if not isinstance(translations, dict):
            raise ModelResponseError("token_translation_invalid", "模型返回的 translations 必须是包含 positive 数组的对象。")
        positive_translations = _translation_list(raw.get("positive_translations", translations.get("positive")), "positive_translations")
        if len(positive_translations) != len(document["positive_tokens"]):
            raise ModelResponseError("token_translation_count_mismatch", "正面中文翻译数量必须与正面 Token 数量一致，不能合并、遗漏或重排。")
        for index, token in enumerate(document["positive_tokens"]):
            raw_text = token["raw_text"]
            translation = positive_translations[index]
            protected = bool(token.get("locked") or PROTECTED_RE.match(raw_text))
            if protected:
                if translation != raw_text:
                    raise ModelResponseError("protected_translation_changed", f"受保护 Token 未保持原文：{raw_text}")
            elif translation.casefold() == raw_text.casefold():
                raise ModelResponseError("token_translation_copied", f"中文翻译不能直接复制英文原文：{raw_text}")
        document["positive_translations"] = positive_translations
        document["chinese_explanation"] = "，".join(positive_translations)
    else:
        document["positive_translations"] = []
        document["chinese_explanation"] = ""
    document.pop("negative_tokens", None)
    return document


async def generate(body: Any, provider: Any, secret: str, system_prompt: str = "", enabled_skills: Any = None, event_sink: Callable[[dict[str, Any]], None] | None = None, repair_note: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    if not provider:
        return {"status": "failed", "engine": "none", "variants": [], "error": error("provider_unavailable", "没有启用的模型供应商，请先在设置中配置。"), "latency_ms": 0}
    if not secret:
        return {"status": "failed", "engine": "none", "variants": [], "error": error("provider_credentials_missing", "模型供应商缺少 API key。"), "latency_ms": 0}
    variant_required = ["positive_tokens"]
    variant_properties = {
        "title": {"type": "string"},
        "intent": {"type": "string"},
        "positive_tokens": {"type": "array"},
        "protected_tokens": {"type": "array"},
        "notes": {"type": "string"},
    }
    if body.include_chinese:
        variant_required.append("positive_translations")
        variant_properties["positive_translations"] = {"type": "array", "items": {"type": "string", "minLength": 1}, "description": "Simplified Chinese translation of each positive token, same length and order as positive_tokens"}
    schema = {
        "type": "object",
        "required": ["variants"],
        "properties": {
            "variants": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "object", "required": variant_required, "properties": variant_properties},
            }
        },
    }
    fallback_count = len((body.current_document or {}).get("variants", [])) if getattr(body, "mode", "create") == "modify" else 1
    parse_intent = (body.current_document or {}).get("modification_request", "") if getattr(body, "mode", "create") == "modify" else body.intent
    parsed_request = parse_generation_request(parse_intent or body.intent, fallback_count=fallback_count)
    vague_multi = bool(re.search(r"多组|几组|若干组|multiple\s+(?:prompts?|sets?|variants?)|several\s+(?:prompts?|sets?|variants?)", parse_intent or body.intent, flags=re.I))
    agent_request = await _agent_parse_request(parse_intent or body.intent, provider, secret, fallback_count, body.model) if vague_multi and not parsed_request["explicit_count"] else None
    if agent_request:
        if not parsed_request["explicit_count"]:
            parsed_request["requested_count"] = agent_request["requested_count"]
            parsed_request["explicit_count"] = agent_request["explicit_count"]
        parsed_request["variation_dimensions"] = list(dict.fromkeys(parsed_request["variation_dimensions"] + agent_request["variation_dimensions"]))
        parsed_request["dedupe_required"] = bool(parsed_request["dedupe_required"] or agent_request["dedupe_required"])
    legacy_count = getattr(body, "requested_count", None)
    requested_count = parsed_request["requested_count"] if parsed_request["explicit_count"] else int(legacy_count or parsed_request["requested_count"])
    if getattr(body, "mode", "create") == "modify" and not parsed_request["explicit_count"]:
        requested_count = fallback_count or 1
    variation_dimensions = parsed_request["variation_dimensions"]
    dedupe_required = parsed_request["dedupe_required"]
    custom_prompt = str(system_prompt or "").strip()[:12000]
    system = (custom_prompt + "\n\n" if custom_prompt else "") + STUDIO_PERSONA + "\n\n" + DEFAULT_SYSTEM_PROMPT + "\n"
    if body.include_chinese:
        system += TRANSLATION_RULE + "\n"
    skill_state = dict(enabled_skills) if isinstance(enabled_skills, dict) else {}
    skill_state["__mode"] = "compact"
    skill_state["__intent"] = skill_state.get("__intent") or (
        f"{body.intent} {(body.current_document or {}).get('modification_request') or ''}".strip()
        if getattr(body, "mode", "create") == "modify"
        else body.intent
    )
    skill_state["__requested_count"] = requested_count
    skill_state["__variation_dimensions"] = variation_dimensions
    skill_state["__dedupe_required"] = dedupe_required
    if "__explicit_skill_ids" not in skill_state:
        skill_state["__explicit_skill_ids"] = list((body.current_document or {}).get("_explicit_skill_ids") or [])
    actual_skill_ids = selected_skill_ids(skill_state)
    injected_skill_ids = set(actual_skill_ids)
    rendered = skill_instructions(skill_state)
    if rendered:
        system += "\nApply these selected repository Skills for this request:\n" + "\n\n".join(rendered) + "\n"
    if dedupe_required:
        system += "\nThis request requires distinct variants. Vary the requested semantic dimensions and avoid repeating core tags across variants; do not collapse variants into one.\n"
    if repair_note:
        system += "\n" + repair_note.strip() + "\n"
    system += (
        "\nSelected Skill indexes are already present above. Call read_skill with section to load a tag catalog. "
        "Before returning any JSON, you MUST call validate_prompt with enforce_quantity=true. "
        "Only after it passes, return the final JSON with variants.\n"
    )
    system += f"Return this shape: {json.dumps(schema, ensure_ascii=False)}"
    selected_model = body.model or provider["model"]
    completion_limit = int(provider["max_tokens"])
    if body.reasoning_effort != "none":
        completion_limit = max(completion_limit, 4096)
    user_payload = {"intent": body.intent, "current_document": body.current_document, "requested_count": requested_count, "include_chinese": body.include_chinese, "variation_dimensions": variation_dimensions, "dedupe_required": dedupe_required}
    if getattr(body, "mode", "create") == "modify":
        system += "\nThis is a modification request. Treat current_document.variants as the complete current conversation output. Apply only the user's modification_request; preserve all unspecified subjects, composition, camera, style, and tokens. Return the full revised variants array, not a diff.\n"
        user_payload["original_intent"] = body.current_document.get("original_intent", body.intent)
        user_payload["modification_request"] = body.current_document.get("modification_request", "")
    request = {
        "model": selected_model,
        "temperature": provider["temperature"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "tools": _tool_definitions(),
        "tool_choice": "auto",
    }
    token_key = "max_completion_tokens" if selected_model.lower().startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
    request[token_key] = completion_limit
    if body.reasoning_effort != "none":
        request["reasoning_effort"] = body.reasoning_effort
    last_usage: dict[str, Any] = {}
    messages = request["messages"]
    trace: list[dict[str, Any]] = []
    tool_call_count = 0
    def emit(event: dict[str, Any]) -> None:
        trace.append(event)
        if event_sink:
            event_sink(event)
    try:
        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT_SECONDS) as client:
            deadline = time.perf_counter() + AGENT_TIMEOUT_SECONDS
            for round_index in range(MAX_AGENT_ROUNDS):
                if time.perf_counter() >= deadline:
                    raise ModelResponseError("agent_timeout", "Agent 执行超过 300 秒。")
                request["messages"] = messages
                emit({
                    "event_type": "model_request",
                    "round": round_index + 1,
                    "model": selected_model,
                    "message_count": len(messages),
                    "system_prompt_chars": len(str(messages[0].get("content") or "")) if messages else 0,
                    "tool_count": len(trace),
                })
                remaining = max(0.1, deadline - time.perf_counter())
                response = await asyncio.wait_for(client.post(provider["base_url"].rstrip("/") + "/chat/completions", json=request, headers={"Authorization": f"Bearer {secret}"}), timeout=remaining)
                response.raise_for_status()
                payload = response.json()
                choice = payload["choices"][0]
                message = choice["message"]
                last_usage = payload.get("usage") or {}
                calls = _tool_call_items(message)
                if calls:
                    messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": message.get("tool_calls")})
                    for call in calls:
                        tool_call_count += 1
                        if tool_call_count > MAX_TOOL_CALLS:
                            raise ModelResponseError("tool_call_limit", f"Agent 超过单次 Run 工具调用上限 {MAX_TOOL_CALLS}。")
                        started_tool = time.perf_counter()
                        try:
                            result = _execute_tool(call["name"], call["arguments"], injected_skill_ids)
                            tool_payload = {"ok": True, "result": _clip(result)}
                            event_status = "completed"
                        except (TypeError, ValueError, KeyError) as exc:
                            tool_payload = {"ok": False, "error": error("tool_error", str(exc))}
                            event_status = "failed"
                        emit({"event_type": "tool_call", "round": round_index + 1, "tool_name": call["name"], "arguments": _clip(call["arguments"], 4000), "result": tool_payload, "status": event_status, "latency_ms": int((time.perf_counter() - started_tool) * 1000)})
                        messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": json.dumps(tool_payload, ensure_ascii=False)})
                    continue
                content = _message_content(message)
                try:
                    if not content:
                        finish_reason = str(choice.get("finish_reason") or "unknown")
                        reasoning = _content_text(message.get("reasoning_content", message.get("reasoning")))
                        if finish_reason in {"length", "max_tokens"}:
                            detail = f"供应商因完成 Token 上限停止（finish_reason={finish_reason}，当前上限 {completion_limit}）；请提高供应商完成 Token 上限。"
                        elif reasoning:
                            detail = "供应商把输出放在思考字段，但其中没有可解析的 variants JSON；请重试或切换模型。"
                        else:
                            detail = f"供应商返回成功响应但正文为空（finish_reason={finish_reason}）；请检查该模型的 OpenAI 兼容输出。"
                        raise ModelResponseError("provider_empty_content", detail)
                    parsed = _parse_model_json(content)
                    if not isinstance(parsed.get("variants"), list):
                        raise ModelResponseError("provider_schema_invalid", "模型返回结果缺少 variants 数组。")
                    raw_variants = parsed["variants"]
                    if len(raw_variants) < requested_count:
                        raise ModelResponseError("variant_count_insufficient", f"用户要求 {requested_count} 组，但模型只返回 {len(raw_variants)} 组。")
                    variants = [validate_variant(item, body.include_chinese) for item in raw_variants[:requested_count]]
                    if not variants:
                        raise ModelResponseError("provider_schema_invalid", "模型没有返回有效候选。")
                    diagnostics = _cross_variant_diagnostics(variants, variation_dimensions) if requested_count > 1 else []
                    if diagnostics:
                        if round_index + 1 < MAX_AGENT_ROUNDS:
                            raise ModelResponseError("variant_too_similar", "; ".join(item["code"] + " variants " + str(item["variants"]) for item in diagnostics))
                        input_tokens, output_tokens = _usage_tokens(last_usage)
                        emit({"event_type": "final", "round": round_index + 1, "status": "completed"})
                        return {"status": "completed", "engine": "openai-compatible", "variants": variants, "error": None, "latency_ms": int((time.perf_counter() - started) * 1000), "input_tokens": input_tokens, "output_tokens": output_tokens, "tool_trace": trace, "selected_skill_ids": actual_skill_ids, "variant_diagnostics": diagnostics}
                except (ModelResponseError, KeyError, TypeError, ValueError) as exc:
                    retry_note = f"Return exactly {requested_count} complete variants in one JSON object. Follow the required schema, quantity band, and keep variant overlap below {VARIANT_OVERLAP_LIMIT}."
                    messages.extend([
                        {"role": "assistant", "content": content[:12000]},
                        {"role": "user", "content": f"Your response failed validation: {str(exc)[:500]} {retry_note}"},
                    ])
                    if round_index + 1 < MAX_AGENT_ROUNDS:
                        continue
                    raise
                input_tokens, output_tokens = _usage_tokens(last_usage)
                emit({"event_type": "final", "round": round_index + 1, "status": "completed"})
                return {"status": "completed", "engine": "openai-compatible", "variants": variants, "error": None, "latency_ms": int((time.perf_counter() - started) * 1000), "input_tokens": input_tokens, "output_tokens": output_tokens, "tool_trace": trace, "selected_skill_ids": actual_skill_ids, "variant_diagnostics": []}
            raise ModelResponseError("agent_loop_limit", f"Agent 超过最大循环轮数 {MAX_AGENT_ROUNDS}。")
    except ModelResponseError as exc:
        run_error = error(exc.code, str(exc))
    except asyncio.TimeoutError as exc:
        run_error = error("agent_timeout", "Agent 执行超过 300 秒。")
    except httpx.TimeoutException as exc:
        run_error = error("provider_timeout", str(exc))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        run_error = error("provider_response_invalid", str(exc))
    input_tokens, output_tokens = _usage_tokens(last_usage)
    emit({"event_type": "error", "status": "failed", "error": run_error})
    return {"status": "failed", "engine": "openai-compatible", "variants": [], "error": run_error, "latency_ms": int((time.perf_counter() - started) * 1000), "input_tokens": input_tokens, "output_tokens": output_tokens, "tool_trace": trace}
