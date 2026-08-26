"""Canonical prompt ban lists shared by validation and the built-in persona."""
from __future__ import annotations

QUALITY_BANNED_TOKENS = frozenset({
    "masterpiece",
    "best quality",
    "score_9",
})

FORBIDDEN_SECTION_13_6 = frozenset({
    "sunlight", "moonlight", "dim light", "candlelight", "neon light", "neon lights", "streetlights",
    "backlighting", "rim light", "warm lighting", "cool lighting", "golden hour glow", "soft lighting",
    "warm tone", "cool tone", "sepia", "blue tone", "amber tone", "god rays", "light rays",
    "light particles", "volumetric light beams", "tyndall effect", "glowing", "illuminated", "lit",
    "backlit", "spotlight", "flash",
})

GENERIC_BANNED_TOKENS = frozenset({
    "style",
    "anime style",
    "illustration style",
    "detailed",
})

BANLIST = {
    "quality": QUALITY_BANNED_TOKENS,
    "section_13_6": FORBIDDEN_SECTION_13_6,
    "generic": GENERIC_BANNED_TOKENS,
}


def format_tokens(tokens: frozenset[str]) -> str:
    """Return a stable, readable list for prompt instructions."""
    return ", ".join(sorted(tokens))
