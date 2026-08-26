"""Local secret storage with Windows Credential Manager when available."""
from __future__ import annotations

import os
from typing import Any

SERVICE = "anima-prompt-workbench"


def _keyring() -> Any | None:
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


def get_secret(ref: str, *, env_name: str = "") -> str:
    if ref:
        ring = _keyring()
        if ring:
            try:
                value = ring.get_password(SERVICE, ref)
                if value:
                    return value
            except Exception:
                pass
    return os.getenv(env_name, "") if env_name else ""


def put_secret(ref: str, value: str) -> bool:
    if not value:
        return True
    ring = _keyring()
    if not ring:
        return False
    try:
        ring.set_password(SERVICE, ref, value)
        return True
    except Exception:
        return False


def delete_secret(ref: str) -> None:
    if not ref:
        return
    ring = _keyring()
    if not ring:
        return
    try:
        ring.delete_password(SERVICE, ref)
    except Exception:
        pass
