"""Prompt tokenization and Anima-safe serialization."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

SPECIAL = re.compile(r"(<lora:[^>]+>|<embed:[^>]+>|\bBREAK\b)", re.I)
WEIGHTED = re.compile(r"^\((.*?):([0-9]+(?:\.[0-9]+)?)\)$", re.S)
SCORE = re.compile(r"^score[ _-]?([1-9])(?:[ _-]+up)?$", re.I)
YEAR = re.compile(r"^(?:year\s+)?((?:19|20)\d{2})$", re.I)
TAG_SHAPE = re.compile(r"^[\w@+\\/\-;' ]+$", re.UNICODE)


@dataclass
class PromptToken:
    id: str
    raw_text: str
    normalized_tag: str
    category: str = "Custom"
    weight: float = 1.0
    source: str = "manual"
    translation: str = ""
    locked: bool = False

    def json(self) -> dict:
        return asdict(self)


def split_prompt(text: str) -> list[str]:
    """Split commas outside parentheses while keeping special tokens intact."""
    parts, current, depth = [], [], 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            value = "".join(current).strip()
            if value:
                parts.append(value)
            current = []
        else:
            current.append(char)
    value = "".join(current).strip()
    if value:
        parts.append(value)
    return parts


def _weight(value: str) -> tuple[str, float]:
    match = WEIGHTED.match(value.strip())
    if match:
        return match.group(1).strip(), float(match.group(2))
    return value.strip(), 1.0


def classify(tag: str) -> str:
    low = tag.lower()
    if low.startswith("<lora:"):
        return "LoRA / Embedding"
    if low.startswith("<embed:"):
        return "LoRA / Embedding"
    if low == "break":
        return "Composition / Camera"
    if any(x in low for x in ("negative", "bad anatomy", "worst quality", "low quality")):
        return "Negative"
    if any(x in low for x in ("masterpiece", "best quality", "score_", "highres")):
        return "Quality"
    if any(x in low for x in ("girl", "boy", "character", "solo", "multiple")):
        return "Character"
    if any(x in low for x in ("hair", "eyes", "skin", "smile", "face")):
        return "Appearance"
    if any(x in low for x in ("dress", "shirt", "skirt", "uniform", "clothes")):
        return "Clothing"
    if any(x in low for x in ("standing", "sitting", "pose", "looking", "arms")):
        return "Pose / Action"
    if any(x in low for x in ("artist:", "style", "watercolor", "oil painting", "lineart")):
        return "Style / Medium"
    if any(x in low for x in ("background", "outdoors", "indoors", "sky", "forest", "room")):
        return "Background / Scene"
    if any(x in low for x in ("lighting", "glow", "shadow", "sparkle")):
        return "Lighting / Effect"
    return "Custom"


def parse_prompt(text: str, prefix: str = "p") -> list[PromptToken]:
    tokens = []
    for index, value in enumerate(split_prompt(text)):
        tag, weight = _weight(value)
        tokens.append(PromptToken(f"{prefix}-{index + 1}", value, tag.replace("_", " "), classify(tag), weight))
    return tokens


def serialize_prompt(tokens: Iterable[PromptToken | dict]) -> str:
    output = []
    for item in tokens:
        token = item if isinstance(item, PromptToken) else PromptToken(**item)
        value = token.raw_text.strip()
        if token.weight != 1.0 and not value.startswith("("):
            value = f"({value}:{token.weight:g})"
        output.append(value)
    return ", ".join(output)


def protected_text(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}
    def replace(match: re.Match[str]) -> str:
        key = f"__ANIMA_PROTECTED_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key
    return SPECIAL.sub(replace, text), placeholders


def restore_protected(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def normalize_part(value: str, *, artist: bool = False, protected: set[str] | None = None) -> tuple[str, list[str]]:
    """Apply the fixed Anima-safe normalization rules, leaving natural sentences alone."""
    original = value.strip()
    if not original:
        return "", []
    if protected and original.casefold() in {x.casefold() for x in protected}:
        return original, []
    if original.upper() == "BREAK":
        return "BREAK", []
    weighted = WEIGHTED.match(original)
    if weighted:
        inner, changes = normalize_part(weighted.group(1), artist=artist, protected=protected)
        return f"({inner}:{weighted.group(2)})", changes
    changes: list[str] = []
    value = original
    score = SCORE.match(value)
    if score:
        normalized = f"score_{score.group(1)}" + ("_up" if "up" in value.lower() else "")
        if normalized != value:
            value = normalized; changes.append("score-format")
    else:
        year = YEAR.match(value)
        if year:
            normalized = f"year {year.group(1)}"
            if normalized != value:
                value = normalized; changes.append("year-format")
    if artist:
        normalized = "@" + value.lstrip("@").removeprefix("by ").strip()
        if normalized != value:
            value = normalized; changes.append("artist-prefix")
    if TAG_SHAPE.fullmatch(value) and len(value.split()) <= 12 and not any(mark in value for mark in ".!?。！？"):
        if not value.lower().startswith("score_"):
            normalized = value.replace("_", " ")
            if normalized != value:
                value = normalized; changes.append("underscores-to-spaces")
        normalized = value.lower()
        if normalized != value:
            value = normalized; changes.append("lowercase-tags")
    return value, changes


def normalize_prompt_text(text: str, *, protected_lora: Iterable[str] = ()) -> tuple[str, list[dict]]:
    """Normalize top-level prompt parts and return an auditable change list."""
    parts = split_prompt(text)
    output: list[str] = []
    changes: list[dict] = []
    seen: set[str] = set()
    protected = set(protected_lora)
    for part in parts:
        normalized, rules = normalize_part(part, protected=protected)
        if not normalized:
            changes.append({"rule": "empty-item", "before": part, "after": None})
            continue
        identity = normalized.casefold()
        if identity in seen:
            changes.append({"rule": "deduplicate", "before": part, "after": None})
            continue
        seen.add(identity)
        if normalized != part:
            changes.append({"rule": ",".join(rules) or "format", "before": part, "after": normalized})
        output.append(normalized)
    normalized_text = ", ".join(output)
    if normalized_text != text:
        changes.append({"rule": "format-separators", "before": text, "after": normalized_text})
    return normalized_text, changes
