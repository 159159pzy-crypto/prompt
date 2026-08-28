"""Codex-compatible Skill discovery and activation.

Skills are instruction folders under ``.agents/skills``: one ``SKILL.md`` per
folder following the Codex skill format (learn.chatgpt.com/docs/build-skills.md)
— YAML frontmatter with a lowercase ``name`` and a ``description``, then the
instruction body. Optional frontmatter:

- ``triggers``: phrases that activate the skill (the only implicit matcher)
- ``depends_on``: other skill ids to inject when this skill is selected
- ``default_enabled``: catalog default; core skills stay injected regardless
- ``sections``: named reference files under ``references/<id>.md``

An optional ``agents/openai.yaml`` may set ``policy.allow_implicit_invocation: false``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR_NAME = ".agents/skills"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EXPLICIT_RE = re.compile(r"(?<!\S)\$([A-Za-z0-9][A-Za-z0-9_-]*)")


@dataclass(frozen=True)
class RepositorySkill:
    id: str
    name: str
    display_name: str
    description: str
    path: Path
    allow_implicit_invocation: bool = True
    default_enabled: bool = True
    triggers: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()

    def catalog_item(self, enabled: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name or self.name,
            "description": self.description,
            "enabled": enabled,
            "source": "codex",
            "path": str(self.path),
            "allow_implicit_invocation": self.allow_implicit_invocation,
            "default_enabled": self.default_enabled,
            "triggers": list(self.triggers),
            "depends_on": list(self.depends_on),
            "sections": list(self.sections),
        }


def _diagnostic(code: str, path: Path, message: str) -> dict[str, str]:
    return {"code": code, "path": str(path), "message": message}


def _frontmatter(text: str, path: Path) -> tuple[dict[str, Any], int]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("YAML frontmatter closing delimiter is missing")
    raw = "".join(lines[1:end])
    metadata = yaml.safe_load(raw) or {}
    if not isinstance(metadata, dict):
        raise ValueError("YAML frontmatter must be an object")
    return metadata, sum(len(line) for line in lines[: end + 1])


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"{field} must be a string or a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return tuple(result)


def _parse_skill(path: Path) -> RepositorySkill:
    text = path.read_text(encoding="utf-8")
    metadata, _ = _frontmatter(text, path)
    name = metadata.get("name")
    description = metadata.get("description")
    display_name = metadata.get("display_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("frontmatter requires a non-empty name")
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("name must use lowercase letters, numbers, hyphens, or underscores")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("frontmatter requires a non-empty description")
    description = description.strip()
    if not isinstance(display_name, str):
        display_name = ""
    display_name = display_name.strip()
    default_enabled = True if "default_enabled" not in metadata else bool(metadata.get("default_enabled"))
    triggers = _string_tuple(metadata.get("triggers"), "triggers")
    depends_on = _string_tuple(metadata.get("depends_on"), "depends_on")
    sections = _string_tuple(metadata.get("sections"), "sections")
    allow_implicit = True
    openai_yaml = path.parent / "agents" / "openai.yaml"
    if openai_yaml.is_file():
        metadata_yaml = yaml.safe_load(openai_yaml.read_text(encoding="utf-8")) or {}
        if not isinstance(metadata_yaml, dict):
            raise ValueError("agents/openai.yaml must be an object")
        policy = metadata_yaml.get("policy", {})
        if policy is not None and not isinstance(policy, dict):
            raise ValueError("agents/openai.yaml policy must be an object")
        if isinstance(policy, dict) and "allow_implicit_invocation" in policy:
            allow_implicit = bool(policy["allow_implicit_invocation"])
    return RepositorySkill(
        name, name, display_name, description, path.resolve(),
        allow_implicit, default_enabled, triggers, depends_on, sections,
    )


@lru_cache(maxsize=None)
def _discover_cached(root: Path) -> tuple[tuple[RepositorySkill, ...], tuple[tuple[str, str, str], ...]]:
    skills_dir = root / SKILLS_DIR_NAME
    if not skills_dir.is_dir():
        return (), ()
    skills: list[RepositorySkill] = []
    diagnostics: list[tuple[str, str, str]] = []
    seen: dict[str, Path] = {}
    for entry in sorted(skills_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            diagnostics.append(("missing_skill_file", str(entry), "Skill directory is missing SKILL.md"))
            continue
        try:
            skill = _parse_skill(skill_file)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            diagnostics.append(("invalid_skill", str(skill_file), str(exc)))
            continue
        key = skill.name.casefold()
        if key in seen:
            diagnostics.append(("duplicate_skill_name", str(skill_file), f"Skill name conflicts with {seen[key]}"))
            continue
        seen[key] = skill_file
        skills.append(skill)
    return tuple(skills), tuple(diagnostics)


def discover(repo_root: Path | None = None) -> tuple[list[RepositorySkill], list[dict[str, str]]]:
    root = (repo_root or REPO_ROOT).resolve()
    skills, diagnostics = _discover_cached(root)
    return list(skills), [{"code": code, "path": path, "message": message} for code, path, message in diagnostics]


def catalog(value: Any = None, repo_root: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    enabled = value if isinstance(value, dict) else {}
    discovered, diagnostics = discover(repo_root)
    return [item.catalog_item(bool(enabled.get(item.id, item.default_enabled))) for item in discovered], diagnostics


def _trigger_match(trigger: str, intent: str) -> bool:
    needle = trigger.strip()
    if not needle:
        return False
    haystack = intent.casefold()
    folded = needle.casefold()
    if any("\u3400" <= char <= "\u9fff" for char in needle):
        return folded in haystack
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(folded) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, haystack) is not None


def _implicit_match(skill: RepositorySkill, intent: str) -> bool:
    if not skill.allow_implicit_invocation or not skill.triggers:
        return False
    return any(_trigger_match(trigger, intent) for trigger in skill.triggers)


def matching_triggers(skill: RepositorySkill, intent: str) -> list[str]:
    return [trigger for trigger in skill.triggers if _trigger_match(trigger, intent)]


def explicit_names(intent: str) -> set[str]:
    """Names referenced with the explicit ``$skill-name`` marker."""
    return {match.group(1).casefold() for match in _EXPLICIT_RE.finditer(intent)}


def strip_explicit_markers(intent: str, repo_root: Path | None = None) -> str:
    """Remove known ``$skill-name`` markers; unknown markers stay verbatim."""
    known = {skill.name.casefold() for skill in discover(repo_root)[0]}

    def replace(match: re.Match[str]) -> str:
        return "" if match.group(1).casefold() in known else match.group(0)

    return re.sub(r"\s{2,}", " ", _EXPLICIT_RE.sub(replace, intent)).strip()


def activate(intent: str, enabled: Any = None, repo_root: Path | None = None) -> dict[str, Any]:
    discovered, diagnostics = discover(repo_root)
    enabled_map = enabled if isinstance(enabled, dict) else {}
    by_name = {item.name.casefold(): item for item in discovered}
    selected: list[RepositorySkill] = []
    explicit: set[str] = set()

    def replace_explicit(match: re.Match[str]) -> str:
        name = match.group(1)
        skill = by_name.get(name.casefold())
        if skill is None:
            diagnostics.append(_diagnostic("unknown_skill", Path("$" + name), f"Unknown Skill: {name}"))
            return match.group(0)
        explicit.add(skill.name.casefold())
        return ""

    cleaned_intent = _EXPLICIT_RE.sub(replace_explicit, intent)
    for skill in discovered:
        if skill.name.casefold() in explicit:
            selected.append(skill)
            continue
        if not bool(enabled_map.get(skill.id, skill.default_enabled)):
            continue
        if _implicit_match(skill, intent):
            selected.append(skill)
    return {
        "intent": re.sub(r"\s{2,}", " ", cleaned_intent).strip(),
        "selected_skill_ids": [item.id for item in selected],
        "selected": selected,
        "diagnostics": diagnostics,
    }


def load_instructions(skill: RepositorySkill) -> str:
    text = skill.path.read_text(encoding="utf-8")
    _, body_start = _frontmatter(text, skill.path)
    return text[body_start:].strip()


def list_sections(skill: RepositorySkill) -> list[dict[str, str]]:
    folder = skill.path.parent / "references"
    items: list[dict[str, str]] = []
    for section in skill.sections:
        path = folder / f"{section}.md"
        items.append({
            "id": section,
            "available": path.is_file(),
            "chars": path.stat().st_size if path.is_file() else 0,
        })
    return items


def load_section(skill: RepositorySkill, section: str) -> str:
    name = str(section or "").strip()
    if name not in skill.sections:
        raise ValueError(f"unknown section for {skill.id}: {section}")
    path = skill.path.parent / "references" / f"{name}.md"
    if not path.is_file():
        raise ValueError(f"missing reference file: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_selected(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    rendered: list[str] = []
    for item in value:
        if isinstance(item, RepositorySkill):
            skill = item
        elif isinstance(item, dict) and item.get("path"):
            skill = RepositorySkill(
                str(item.get("id") or item.get("name")),
                str(item.get("name") or item.get("id")),
                str(item.get("display_name") or ""),
                str(item.get("description") or ""),
                Path(str(item["path"])),
                bool(item.get("allow_implicit_invocation", True)),
                bool(item.get("default_enabled", True)),
                tuple(item.get("triggers") or ()),
                tuple(item.get("depends_on") or ()),
                tuple(item.get("sections") or ()),
            )
        else:
            continue
        body = load_instructions(skill)
        if body:
            rendered.append(f"Repository Skill {skill.name} ({skill.path}):\n{body}")
    return rendered
