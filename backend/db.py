"""Small SQLite store for the single-user Anima prompt workbench."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .skills import default_enabled

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "workbench.sqlite3"
SCHEMA_VERSION = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def row_json(row: sqlite3.Row | None) -> dict:
    return dict(row) if row else {}


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompt_documents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            intent TEXT NOT NULL DEFAULT '',
            positive_tokens TEXT NOT NULL DEFAULT '[]',
            negative_tokens TEXT NOT NULL DEFAULT '[]',
            protected_tokens TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            source_run_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(prompt_id) REFERENCES prompt_documents(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt
            ON prompt_versions(prompt_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            intent TEXT NOT NULL,
            request_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            error_json TEXT NOT NULL DEFAULT '{}',
            engine TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 4096,
            timeout INTEGER NOT NULL DEFAULT 120,
            enabled INTEGER NOT NULL DEFAULT 1,
            secret_ref TEXT NOT NULL DEFAULT '',
            models_json TEXT NOT NULL DEFAULT '[]',
            models_synced_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(providers)")}
    if "models_json" not in columns:
        db.execute("ALTER TABLE providers ADD COLUMN models_json TEXT NOT NULL DEFAULT '[]'")
    if "models_synced_at" not in columns:
        db.execute("ALTER TABLE providers ADD COLUMN models_synced_at TEXT NOT NULL DEFAULT ''")
    db.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
        (str(SCHEMA_VERSION),),
    )


def _seed(db: sqlite3.Connection) -> None:
    defaults = {
        "runtime": {
            "requested_count": 1,
            "include_chinese": False,
            "system_prompt": "",
            "provider_id": "",
            "model": "",
            "reasoning_effort": "none",
            "skill_mode": "compact",
            "skills": default_enabled(),
        },
    }
    for key, payload in defaults.items():
        row = db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO settings(key,payload,updated_at) VALUES(?,?,?)",
                (key, json.dumps(payload, ensure_ascii=False), now()),
            )
            continue
        try:
            current = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        merged = {**payload, **current}
        if key == "runtime":
            merged = {field: merged.get(field, default) for field, default in payload.items()}
        if merged != current:
            db.execute("UPDATE settings SET payload=?,updated_at=? WHERE key=?", (json.dumps(merged, ensure_ascii=False), now(), key))
    # Remove settings from the pre-v2 contract that had no runtime consumer.
    db.execute("DELETE FROM settings WHERE key='anima_rules'")


def init_db() -> None:
    with connect() as db:
        _create_schema(db)
        _seed(db)
        db.commit()
