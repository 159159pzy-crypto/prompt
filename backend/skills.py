"""Toggleable Anima skills backed by Codex-format SKILL.md files.

Every built-in Skill lives under ``.agents/skills/<name>/SKILL.md``. This
module adds the studio selection model on top of ``skill_runtime``:

- core rule Skills are always injected
- compact mode adds a library when the intent matches its ``triggers``,
  an explicit ``$skill-name`` marker, a requested variation dimension,
  or a ``depends_on`` edge from an already selected skill
- description text is never used as a matcher
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import skill_runtime

CORE_SKILL_IDS = ("anima-tags", "token-protection", "slot-order", "conflict-check", "assembly-tree")
LIBRARY_SKILL_IDS = (
    "appearance-library",
    "clothing-library",
    "pose-library",
    "expression-library",
    "camera-scene-library",
    "mood-library",
    "special-themes",
)

_DEFINITIONS_CACHE: dict[Path, tuple[dict[str, Any], ...]] = {}


def definitions() -> tuple[dict[str, Any], ...]:
    """Codex Skills discovered from .agents/skills, core rules first."""
    root = Path(skill_runtime.REPO_ROOT).resolve()
    if root not in _DEFINITIONS_CACHE:
        discovered, _ = skill_runtime.discover(root)
        items = [
            {
                "id": skill.name,
                "name": skill.display_name or skill.name,
                "description": skill.description,
                "instruction": skill_runtime.load_instructions(skill),
                "default_enabled": skill.default_enabled,
                "path": str(skill.path),
                "allow_implicit_invocation": skill.allow_implicit_invocation,
                "triggers": skill.triggers,
                "depends_on": skill.depends_on,
                "sections": skill.sections,
                "skill": skill,
            }
            for skill in discovered
        ]
        core = set(CORE_SKILL_IDS)
        items.sort(key=lambda item: (0 if item["id"] in core else 1, item["id"]))
        _DEFINITIONS_CACHE[root] = tuple(items)
    return _DEFINITIONS_CACHE[root]


def discovery_diagnostics() -> list[dict[str, str]]:
    _, diagnostics = skill_runtime.discover()
    return diagnostics


def default_enabled() -> dict[str, bool]:
    return {item["id"]: bool(item["default_enabled"]) for item in definitions()}


def normalize_enabled(value: Any) -> dict[str, bool]:
    result = default_enabled()
    if isinstance(value, dict):
        for item in definitions():
            if item["id"] in value:
                result[item["id"]] = bool(value[item["id"]])
    return result


def dimension_hints() -> dict[str, tuple[str, ...]]:
    return {item["id"]: item["triggers"] for item in definitions() if item["id"] in LIBRARY_SKILL_IDS and item["triggers"]}


def _expand_dependencies(selected: set[str]) -> set[str]:
    by_id = {item["id"]: item for item in definitions()}
    pending = list(selected)
    while pending:
        current = pending.pop()
        item = by_id.get(current)
        if not item:
            continue
        for dependency in item["depends_on"]:
            if dependency not in selected and dependency in by_id:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def _requested_skill_ids(value: Any) -> set[str]:
    intent = str(value.get("__intent") or "") if isinstance(value, dict) else ""
    selected = set(CORE_SKILL_IDS)
    requested_dimensions = value.get("__variation_dimensions") if isinstance(value, dict) else []
    if isinstance(requested_dimensions, (list, tuple, set)):
        selected.update(str(item) for item in requested_dimensions)
    explicit_ids = value.get("__explicit_skill_ids") if isinstance(value, dict) else []
    if isinstance(explicit_ids, (list, tuple, set)):
        selected.update(str(item) for item in explicit_ids)
    explicit = skill_runtime.explicit_names(intent)
    for item in definitions():
        skill = item["skill"]
        if skill.name.casefold() in explicit:
            selected.add(skill.name)
        elif skill_runtime._implicit_match(skill, intent):
            selected.add(skill.name)
    return _expand_dependencies(selected)


def selected(value: Any) -> list[dict[str, Any]]:
    enabled = normalize_enabled(value)
    compact = isinstance(value, dict) and str(value.get("__mode") or "").casefold() == "compact"
    if compact:
        requested = _requested_skill_ids(value)
        forced: set[str] = set()
        if isinstance(value, dict):
            forced.update(skill_runtime.explicit_names(str(value.get("__intent") or "")))
            extra = value.get("__explicit_skill_ids") or []
            if isinstance(extra, (list, tuple, set)):
                forced.update(str(item).casefold() for item in extra)
        items = [
            item for item in definitions()
            if item["id"] in requested and (
                enabled[item["id"]]
                or item["id"] in CORE_SKILL_IDS
                or item["id"].casefold() in forced
            )
        ]
    else:
        items = [
            item for item in definitions()
            if enabled[item["id"]] or item["id"] in CORE_SKILL_IDS
        ]
    return items


def selected_ids(value: Any) -> list[str]:
    return [item["id"] for item in selected(value)]


def catalog(value: Any) -> list[dict[str, Any]]:
    enabled = normalize_enabled(value)
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "enabled": enabled[item["id"]],
            "path": item["path"],
            "allow_implicit_invocation": item["allow_implicit_invocation"],
            "default_enabled": item["default_enabled"],
            "core": item["id"] in CORE_SKILL_IDS,
            "triggers": list(item["triggers"]),
            "depends_on": list(item["depends_on"]),
            "sections": list(item["sections"]),
        }
        for item in definitions()
    ]


def instructions(value: Any) -> list[str]:
    return [item["instruction"] for item in selected(value)]


def explain_activation(
    intent: str,
    enabled: Any = None,
    *,
    parsed_request: dict[str, Any] | None = None,
    explicit_skill_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Label why each catalog skill is selected, using the same state as generate."""
    from .agent import parse_generation_request

    parsed = parsed_request or parse_generation_request(intent)
    state = build_skill_state(intent, enabled, parsed_request=parsed, explicit_skill_ids=explicit_skill_ids)
    selected_set = set(state.get("__selected_skill_ids") or [])
    explicit = {str(item).casefold() for item in (state.get("__explicit_skill_ids") or [])}
    explicit.update(skill_runtime.explicit_names(intent))
    dimensions = {str(item) for item in (state.get("__variation_dimensions") or [])}
    enabled_map = normalize_enabled(state)
    items = []
    for item in catalog(state):
        skill_id = item["id"]
        definition = next((row for row in definitions() if row["id"] == skill_id), None)
        matched = skill_runtime.matching_triggers(definition["skill"], intent) if definition else []
        if skill_id in CORE_SKILL_IDS:
            reason = "core"
        elif not enabled_map.get(skill_id, True) and skill_id not in CORE_SKILL_IDS and skill_id not in selected_set:
            reason = "disabled"
        elif skill_id in dimensions:
            reason = "dimension"
        elif matched:
            reason = "trigger"
        elif skill_id.casefold() in explicit:
            reason = "explicit"
        elif skill_id in selected_set:
            reason = "dependency"
        else:
            reason = ""
        items.append({**item, "selection_reason": reason, "matched_triggers": matched, "selected": skill_id in selected_set})
    return {"items": items, "selected_skill_ids": list(state.get("__selected_skill_ids") or []), "diagnostics": discovery_diagnostics()}


def build_skill_state(
    intent: str,
    enabled: Any = None,
    *,
    parsed_request: dict[str, Any] | None = None,
    explicit_skill_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Compact skill state shared by /api/generate and the worker."""
    parsed = parsed_request or {}
    state = normalize_enabled(enabled)
    activation = skill_runtime.activate(intent, enabled)
    state["__intent"] = intent
    state["__mode"] = "compact"
    state["__requested_count"] = parsed.get("requested_count", 1)
    state["__variation_dimensions"] = list(parsed.get("variation_dimensions") or [])
    state["__dedupe_required"] = bool(parsed.get("dedupe_required"))
    state["__explicit_skill_ids"] = list(explicit_skill_ids if explicit_skill_ids is not None else activation["selected_skill_ids"])
    state["__selected_skill_ids"] = selected_ids(state)
    return state
