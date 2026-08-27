"""Toggleable Anima skills backed by Codex-format SKILL.md files.

Every built-in Skill lives under ``.agents/skills/<name>/SKILL.md`` (Codex
skill format, learn.chatgpt.com/docs/build-skills.md): YAML frontmatter with
``name``, ``display_name`` and ``description``, followed by the
instruction body. This module adds the studio selection model on top of the
``skill_runtime`` loader:

- the core rule Skills are always injected,
- in compact mode the tag libraries are added when the request intent matches
  their scenario keywords or their description (implicit Codex invocation),
  and an explicit ``$skill-name`` marker always forces a Skill,
- in full mode every enabled Skill is injected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import skill_runtime

CORE_SKILL_IDS = ("anima-tags", "token-protection", "slot-order", "conflict-check", "assembly-tree")

SCENARIO_SKILL_HINTS = {
    "appearance-library": ("外貌", "角色", "发色", "眼睛", "身体", "兽耳", "futa", "男娘"),
    "clothing-library": ("服装", "衣服", "制服", "裙", "内衣", "裸体", "穿着"),
    "pose-library": ("姿势", "体位", "动作", "性交", "口交", "后入", "骑乘", "拥抱"),
    "expression-library": ("表情", "情绪", "哭", "笑", "害羞", "兴奋", "眼神"),
    "camera-scene-library": ("镜头", "构图", "场景", "室内", "街头", "卧室", "摄影", "视角"),
    "mood-library": ("氛围", "质感", "光", "色调", "雨", "雪", "雾", "赛博", "古风"),
    "special-themes": ("特殊", "ntr", "束缚", "偷窥", "调教", "催眠", "群交", "异种"),
}

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
                "default_enabled": True,
                "path": str(skill.path),
                "allow_implicit_invocation": skill.allow_implicit_invocation,
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


def _requested_skill_ids(value: Any) -> set[str]:
    intent = str(value.get("__intent") or "") if isinstance(value, dict) else ""
    lowered = intent.casefold()
    selected = set(CORE_SKILL_IDS)
    if isinstance(value, dict) and bool(value.get("__dedupe_required")):
        selected.add("conflict-check")
        selected.add("slot-order")
    requested_dimensions = value.get("__variation_dimensions") if isinstance(value, dict) else []
    if isinstance(requested_dimensions, (list, tuple, set)):
        selected.update(str(item) for item in requested_dimensions)
    explicit_ids = value.get("__explicit_skill_ids") if isinstance(value, dict) else []
    if isinstance(explicit_ids, (list, tuple, set)):
        selected.update(str(item) for item in explicit_ids)
    for skill_id, hints in SCENARIO_SKILL_HINTS.items():
        if any(str(hint).casefold() in lowered for hint in hints):
            selected.add(skill_id)
    explicit = skill_runtime.explicit_names(intent)
    for item in definitions():
        skill = item["skill"]
        if skill.name.casefold() in explicit:
            selected.add(skill.name)
        elif skill.allow_implicit_invocation and skill_runtime._implicit_match(skill, intent):
            selected.add(skill.name)
    return selected


def selected(value: Any) -> list[dict[str, Any]]:
    enabled = normalize_enabled(value)
    compact = isinstance(value, dict) and str(value.get("__mode") or "").casefold() == "compact"
    if compact:
        requested = _requested_skill_ids(value)
        items = [
            item for item in definitions()
            if item["id"] in requested and (enabled[item["id"]] or item["id"] in CORE_SKILL_IDS)
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
        }
        for item in definitions()
    ]


def instructions(value: Any) -> list[str]:
    return [item["instruction"] for item in selected(value)]
