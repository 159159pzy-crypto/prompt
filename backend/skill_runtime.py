"""Codex-compatible Skill discovery and activation.

Skills are instruction folders under ``.agents/skills``: one ``SKILL.md`` per
folder following the Codex skill format (learn.chatgpt.com/docs/build-skills.md)
— YAML frontmatter with a lowercase ``name`` and a ``description``, then the
instruction body. An optional ``agents/openai.yaml`` may set
``policy.allow_implicit_invocation: false`` to disable keyword activation.

The loader keeps discovery metadata small and reads the instruction body only
for Skills selected for the current request.
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
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CJK_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_STOP_WORDS = {
    "and", "are", "for", "from", "that", "the", "this", "use", "with",
    "when", "only", "into", "your", "you", "not", "should", "skill",
}


@dataclass(frozen=True)
class RepositorySkill:
    id: str
    name: str
    display_name: str
    description: str
    path: Path
    allow_implicit_invocation: bool = True

    def catalog_item(self, enabled: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name or self.name,
            "description": self.description,
            "enabled": enabled,
            "source": "codex",
            "path": str(self.path),
            "allow_implicit_invocation": self.allow_implicit_invocation,
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
    return RepositorySkill(name, name, display_name, description, path.resolve(), allow_implicit)


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
    return [item.catalog_item(bool(enabled.get(item.id, True))) for item in discovered], diagnostics


def _description_terms(description: str) -> list[str]:
    terms = [term.casefold() for term in _WORD_RE.findall(description)]
    terms.extend(term.casefold() for term in _CJK_RE.findall(description))
    return [term for term in dict.fromkeys(terms) if term not in _STOP_WORDS]


def _implicit_match(skill: RepositorySkill, intent: str) -> bool:
    lowered = intent.casefold()
    return any(term in lowered for term in _description_terms(skill.description))


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
        if not bool(enabled_map.get(skill.id, True)):
            continue
        if skill.name.casefold() in explicit or (skill.allow_implicit_invocation and _implicit_match(skill, intent)):
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
            )
        else:
            continue
        body = load_instructions(skill)
        if body:
            rendered.append(f"Repository Skill {skill.name} ({skill.path}):\n{body}")
    return rendered
