from __future__ import annotations

import json
import time
from typing import Any, Callable

from .agent import ValidationFailed, generate as generate_agent, parse_generation_request, validate_variant

STAGES = ("planner", "generator", "validator", "finalizer")


def plan_request(body: Any, enabled_skills: dict[str, Any] | None = None) -> dict[str, Any]:
    current = getattr(body, "current_document", {}) or {}
    intent = str(current.get("modification_request") or getattr(body, "intent", ""))
    fallback = len(current.get("variants", [])) if getattr(body, "mode", "create") == "modify" else 1
    parsed = parse_generation_request(intent, fallback_count=fallback)
    if getattr(body, "requested_count", None):
        parsed["requested_count"] = int(body.requested_count)
        parsed["explicit_count"] = True
    selected = []
    if isinstance(enabled_skills, dict):
        selected = list(enabled_skills.get("__selected_skill_ids") or [])
    return {**parsed, "selected_skill_ids": selected, "mode": getattr(body, "mode", "create")}


async def run_pipeline(body: Any, provider: Any, secret: str, system_prompt: str,
                       enabled_skills: dict[str, Any] | None = None,
                       event_sink: Callable[[dict[str, Any]], None] | None = None,
                       cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    emit = event_sink or (lambda _event: None)
    plan = plan_request(body, enabled_skills)
    emit({"event_type": "stage", "stage": "planner", "step_id": "planner", "status": "completed", "result": plan})
    if cancel_check and cancel_check():
        return {"status": "cancelled", "engine": "runtime", "variants": [], "error": {"code": "run_cancelled", "message": "Run 已取消。"}, "latency_ms": int((time.perf_counter() - started) * 1000)}

    emit({"event_type": "stage", "stage": "generator", "step_id": "generator", "status": "running"})
    result = await generate_agent(body, provider, secret, system_prompt, enabled_skills, event_sink=lambda event: emit({**event, "stage": "generator", "step_id": "generator"}))
    if result.get("status") != "completed":
        return result

    if cancel_check and cancel_check():
        return {"status": "cancelled", "engine": "runtime", "variants": [], "error": {"code": "run_cancelled", "message": "Run 已取消。"}, "latency_ms": int((time.perf_counter() - started) * 1000), "tool_trace": result.get("tool_trace", [])}

    emit({"event_type": "stage", "stage": "validator", "step_id": "validator", "status": "running"})
    validated = []
    try:
        for index, variant in enumerate(result.get("variants", [])):
            try:
                validated.append(validate_variant(variant, bool(getattr(body, "include_chinese", False))))
            except ValidationFailed as exc:
                issues = [{**issue, "variant_index": index} for issue in exc.issues]
                raise ValidationFailed(issues, str(exc)) from exc
    except Exception as exc:
        issues = getattr(exc, "issues", None)
        error_payload = {"code": "validator_failed", "message": str(exc)[:500], **({"issues": issues} if issues else {})}
        emit({"event_type": "stage", "stage": "validator", "step_id": "validator", "status": "failed", "error": error_payload})
        repaired = await generate_agent(
            body, provider, secret, system_prompt, enabled_skills,
            event_sink=lambda event: emit({**event, "stage": "validator", "step_id": "validator", "attempt": 2}),
            repair_note=f"Repair the candidate output so every variant passes deterministic validation: {str(exc)[:500]}. Return only the complete JSON schema.",
        )
        if repaired.get("status") != "completed":
            return repaired
        try:
            validated = []
            for index, item in enumerate(repaired.get("variants", [])):
                try:
                    validated.append(validate_variant(item, bool(getattr(body, "include_chinese", False))))
                except ValidationFailed as repair_exc:
                    repair_issues = [{**issue, "variant_index": index} for issue in repair_exc.issues]
                    raise ValidationFailed(repair_issues, str(repair_exc)) from repair_exc
            result = repaired
        except Exception as repair_exc:
            repair_issues = getattr(repair_exc, "issues", None)
            return {**repaired, "status": "failed", "variants": [], "error": {"code": "validator_failed", "message": str(repair_exc)[:500], **({"issues": repair_issues} if repair_issues else {})}}
    emit({"event_type": "stage", "stage": "validator", "step_id": "validator", "status": "completed", "result": {"count": len(validated)}})

    if cancel_check and cancel_check():
        return {"status": "cancelled", "engine": "runtime", "variants": [], "error": {"code": "run_cancelled", "message": "Run 已取消。"}, "latency_ms": int((time.perf_counter() - started) * 1000)}
    emit({"event_type": "stage", "stage": "finalizer", "step_id": "finalizer", "status": "running"})
    requested = max(1, int(plan.get("requested_count") or 1))
    variants = validated[:requested]
    final = {**result, "variants": variants, "latency_ms": int((time.perf_counter() - started) * 1000)}
    emit({"event_type": "stage", "stage": "finalizer", "step_id": "finalizer", "status": "completed", "result": {"count": len(variants)}})
    return final

