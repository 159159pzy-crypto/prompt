"""Small SQLite store for the single-user Anima prompt workbench."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "workbench.sqlite3"
SCHEMA_VERSION = 5


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
    has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    existing_version = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone() if has_meta else None
    if existing_version and int(existing_version[0] or 0) < 4:
        # Runtime history is intentionally rebuilt for the durable Run model.
        db.execute("DROP TABLE IF EXISTS agent_events")
        db.execute("DROP TABLE IF EXISTS agent_runs")
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
        CREATE TABLE IF NOT EXISTS agent_events (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            tool_name TEXT NOT NULL DEFAULT '',
            arguments_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT '',
            latency_ms INTEGER,
            error_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            step_id TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, sequence);
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
    run_columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_runs)")}
    if "conversation_id" not in run_columns:
        db.execute("ALTER TABLE agent_runs ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
    if "parent_run_id" not in run_columns:
        db.execute("ALTER TABLE agent_runs ADD COLUMN parent_run_id TEXT NOT NULL DEFAULT ''")
    if "revision" not in run_columns:
        db.execute("ALTER TABLE agent_runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "mode" not in run_columns:
        db.execute("ALTER TABLE agent_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'create'")
    for column, definition in {
        "stage": "TEXT NOT NULL DEFAULT 'queued'",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "lease_owner": "TEXT NOT NULL DEFAULT ''",
        "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "started_at": "TEXT NOT NULL DEFAULT ''",
        "finished_at": "TEXT NOT NULL DEFAULT ''",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "progress": "INTEGER NOT NULL DEFAULT 0",
        "usage_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if column not in run_columns:
            db.execute(f"ALTER TABLE agent_runs ADD COLUMN {column} {definition}")
    event_columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_events)")}
    if "step_id" not in event_columns:
        db.execute("ALTER TABLE agent_events ADD COLUMN step_id TEXT NOT NULL DEFAULT ''")
    if "attempt" not in event_columns:
        db.execute("ALTER TABLE agent_events ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE agent_runs SET conversation_id=id WHERE conversation_id='' OR conversation_id IS NULL")

    columns = {row["name"] for row in db.execute("PRAGMA table_info(providers)")}
    if "models_json" not in columns:
        db.execute("ALTER TABLE providers ADD COLUMN models_json TEXT NOT NULL DEFAULT '[]'")
    if "models_synced_at" not in columns:
        db.execute("ALTER TABLE providers ADD COLUMN models_synced_at TEXT NOT NULL DEFAULT ''")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            title_source TEXT NOT NULL DEFAULT 'intent',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_updated
            ON conversations(pinned DESC, updated_at DESC);
        """
    )
    document_columns = {row["name"] for row in db.execute("PRAGMA table_info(prompt_documents)")}
    if "conversation_id" not in document_columns:
        db.execute("ALTER TABLE prompt_documents ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
    if "variant_index" not in document_columns:
        db.execute("ALTER TABLE prompt_documents ADD COLUMN variant_index INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """
        INSERT OR IGNORE INTO conversations (id, title, title_source, pinned, archived_at, created_at, updated_at)
        SELECT id, intent, 'intent', 0, '', created_at, created_at
        FROM (
          SELECT conversation_id AS id, intent, created_at,
                 ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY revision DESC) AS rn
          FROM agent_runs
          WHERE conversation_id <> ''
        )
        WHERE rn = 1
        """
    )
    db.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('version',?)",
        (str(SCHEMA_VERSION),),
    )
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_idempotency ON agent_runs(idempotency_key) WHERE idempotency_key <> ''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_queue ON agent_runs(status, lease_expires_at, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, revision)")


def _seed(db: sqlite3.Connection) -> None:
    defaults = {
        "runtime": {
            "requested_count": 1,
            "include_chinese": False,
            "system_prompt": "",
            "provider_id": "",
            "model": "",
            "reasoning_effort": "none",
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
