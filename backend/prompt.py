"""Anima token parsing and protected syntax helpers."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable

SPECIAL = re.compile(r"(<lora:[^>]+>|<embed:[^>]+>|\bBREAK\b)", re.I)
WEIGHTED = re.compile(r"^\((.*?):([0-9]+(?:\.[0-9]+)?)\)$", re.S)


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
    """Split commas outside parentheses while keeping weighted tokens intact."""
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


def classify(tag: str) -> str:
    low = tag.lower()
    if low.startswith(("<lora:", "<embed:")):
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
        match = WEIGHTED.match(value)
        tag, weight = (match.group(1).strip(), float(match.group(2))) if match else (value.strip(), 1.0)
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
