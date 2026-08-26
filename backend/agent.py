from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from json_repair import repair_json

from .documents import PROTECTED_RE, canonical_document, validate_document
from .prompt import split_prompt
from .skills import instructions as skill_instructions
from .persona import STUDIO_PERSONA


def error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message[:500]}


DEFAULT_SYSTEM_PROMPT = """You create production-ready Anima image prompts. Return JSON only.
The canonical output is structured tokens, never a prose-only prompt.
Each token may be either a string such as {\"raw_text\":\"1girl\",\"weight\":1} or an object with raw_text and weight.
Prefer the object form. Return positive_tokens only. Do not return negative_tokens or a negative prompt."""

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
    issues = validate_document(document)
    if issues:
        raise ValueError("; ".join(issue["message"] for issue in issues))
    if include_chinese:
        translations = raw.get("translations") or {}
        if not isinstance(translations, dict):
            raise ModelResponseError("token_translation_invalid", "模型返回的 translations 必须是包含 positive 数组的对象。")
        positive_translations = _translation_list(raw.get("positive_translations", translations.get("positive")), "positive_translations")
        if len(positive_translations) != len(document["positive_tokens"]):
            raise ModelResponseError("token_translation_count_mismatch", "正面中文翻译数量必须与正面 Token 数量一致，不能合并、遗漏或重排。")
        for index, token in enumerate(document["positive_tokens"]):
            if token.get("locked") or PROTECTED_RE.match(token["raw_text"]):
                if positive_translations[index] != token["raw_text"]:
                    raise ModelResponseError("protected_translation_changed", f"受保护 Token 未保持原文：{token['raw_text']}")
        document["positive_translations"] = positive_translations
        document["chinese_explanation"] = "，".join(positive_translations)
    else:
        document["positive_translations"] = []
        document["chinese_explanation"] = ""
    document.pop("negative_tokens", None)
    return document


async def generate(body: Any, provider: Any, secret: str, system_prompt: str = "", enabled_skills: Any = None) -> dict[str, Any]:
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
    custom_prompt = str(system_prompt or "").strip()[:12000]
    system = (custom_prompt + "\n\n" if custom_prompt else STUDIO_PERSONA + "\n\n") + DEFAULT_SYSTEM_PROMPT + "\n"
    if body.include_chinese:
        system += TRANSLATION_RULE + "\n"
    skill_text = skill_instructions(enabled_skills)
    if skill_text:
        system += "\n".join(skill_text) + "\n"
    system += f"Return this shape: {json.dumps(schema, ensure_ascii=False)}"
    selected_model = body.model or provider["model"]
    completion_limit = int(provider["max_tokens"])
    if body.reasoning_effort != "none":
        completion_limit = max(completion_limit, 4096)
    request = {
        "model": selected_model,
        "temperature": provider["temperature"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"intent": body.intent, "current_document": body.current_document, "requested_count": body.requested_count, "include_chinese": body.include_chinese}, ensure_ascii=False)},
        ],
    }
    token_key = "max_completion_tokens" if selected_model.lower().startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
    request[token_key] = completion_limit
    if body.reasoning_effort != "none":
        request["reasoning_effort"] = body.reasoning_effort
    last_usage: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=provider["timeout"]) as client:
            for attempt in range(2):
                response = await client.post(provider["base_url"].rstrip("/") + "/chat/completions", json=request, headers={"Authorization": f"Bearer {secret}"})
                response.raise_for_status()
                payload = response.json()
                choice = payload["choices"][0]
                message = choice["message"]
                last_usage = payload.get("usage") or {}
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
                    variants = [validate_variant(item, body.include_chinese) for item in parsed["variants"][: body.requested_count]]
                    if not variants:
                        raise ModelResponseError("provider_schema_invalid", "模型没有返回有效候选。")
                except (ModelResponseError, KeyError, TypeError, ValueError) as exc:
                    if attempt:
                        raise
                    retry_note = "Return one complete corrected JSON object only. Follow the required schema and preserve exact token counts."
                    request["messages"].extend([
                        {"role": "assistant", "content": content[:12000]},
                        {"role": "user", "content": f"Your response failed validation: {str(exc)[:500]} {retry_note}"},
                    ])
                    continue
                input_tokens, output_tokens = _usage_tokens(last_usage)
                return {"status": "completed", "engine": "openai-compatible", "variants": variants, "error": None, "latency_ms": int((time.perf_counter() - started) * 1000), "input_tokens": input_tokens, "output_tokens": output_tokens}
    except ModelResponseError as exc:
        run_error = error(exc.code, str(exc))
    except httpx.TimeoutException as exc:
        run_error = error("provider_timeout", str(exc))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        run_error = error("provider_response_invalid", str(exc))
    input_tokens, output_tokens = _usage_tokens(last_usage)
    return {"status": "failed", "engine": "openai-compatible", "variants": [], "error": run_error, "latency_ms": int((time.perf_counter() - started) * 1000), "input_tokens": input_tokens, "output_tokens": output_tokens}
