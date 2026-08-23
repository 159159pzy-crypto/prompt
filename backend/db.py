"""Small SQLite persistence layer for the local single-user workbench."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "workbench.sqlite3"

CATEGORIES = ["Quality", "Style / Medium", "Artist", "Character", "Subject / Count", "Appearance", "Clothing", "Pose / Action", "Composition / Camera", "Background / Scene", "Lighting / Effect", "Negative", "LoRA / Embedding", "Custom"]
SETTING_TREE = {
    "ai": {"label": "AI 与 Agent", "groups": {"agents": "Agent 列表", "defaults": "默认 Agent", "runtime": "运行参数"}},
    "providers": {"label": "供应商与模型", "groups": {"llm": "LLM 供应商", "credentials": "凭据管理"}},
    "translation": {"label": "翻译", "groups": {"engine": "翻译引擎", "google": "Google Cloud Translation", "fallback": "LibreTranslate / Argos", "glossary": "术语表"}},
    "catalog": {"label": "Tag 仓库", "groups": {"sources": "数据源", "merge": "合并优先级", "sync": "同步记录"}},
    "anima": {"label": "Anima 规范", "groups": {"rules": "规则说明", "protection": "触发词保护", "history": "恢复与版本"}},
    "appearance": {"label": "外观与交互", "groups": {"theme": "主题", "motion": "动效与毛玻璃"}},
    "data": {"label": "数据与备份", "groups": {"backup": "导入 / 导出与备份"}},
    "diagnostics": {"label": "诊断", "groups": {"status": "服务状态", "logs": "日志与版本"}},
}
SEED_TAGS = [
    ("masterpiece", "杰作", "Quality"), ("best quality", "最佳质量", "Quality"), ("highres", "高分辨率", "Quality"),
    ("solo", "单人", "Character"), ("1girl", "一个女孩", "Character"), ("1boy", "一个男孩", "Character"),
    ("long hair", "长发", "Appearance"), ("blue eyes", "蓝眼睛", "Appearance"), ("smile", "微笑", "Appearance"),
    ("school uniform", "校服", "Clothing"), ("dress", "连衣裙", "Clothing"), ("standing", "站立", "Pose / Action"),
    ("sitting", "坐姿", "Pose / Action"), ("portrait", "肖像", "Composition / Camera"), ("upper body", "上半身", "Composition / Camera"),
    ("outdoors", "户外", "Background / Scene"), ("blue sky", "蓝天", "Background / Scene"), ("soft lighting", "柔和光线", "Lighting / Effect"),
    ("low quality", "低质量", "Negative"), ("bad anatomy", "糟糕的人体结构", "Negative"), ("<lora:example:0.8>", "示例 LoRA", "LoRA / Embedding"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, title TEXT NOT NULL, positive TEXT NOT NULL, negative TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS favorites (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7, max_tokens INTEGER NOT NULL DEFAULT 1200, timeout INTEGER NOT NULL DEFAULT 30, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, system_prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tags (id TEXT PRIMARY KEY, tag TEXT NOT NULL UNIQUE, translation TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, source TEXT NOT NULL, usage_count INTEGER NOT NULL DEFAULT 0, favorite INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings (section TEXT NOT NULL, group_name TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(section, group_name));
        CREATE TABLE IF NOT EXISTS agent_profiles (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, system_prompt TEXT NOT NULL, persona TEXT NOT NULL, output_schema TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7, max_tokens INTEGER NOT NULL DEFAULT 1600, context_policy TEXT NOT NULL DEFAULT 'conversation', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, message TEXT NOT NULL, response TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS normalization_events (id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, changes_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS catalog_sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, location TEXT NOT NULL, version TEXT NOT NULL DEFAULT 'local', enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100, item_count INTEGER NOT NULL DEFAULT 0, last_sync TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'not_synced');
        CREATE TABLE IF NOT EXISTS tag_aliases (id TEXT PRIMARY KEY, tag_id TEXT NOT NULL, alias TEXT NOT NULL UNIQUE, language TEXT NOT NULL DEFAULT 'en');
        CREATE TABLE IF NOT EXISTS translation_config (id INTEGER PRIMARY KEY CHECK(id=1), primary_engine TEXT NOT NULL DEFAULT 'google', fallback_engines TEXT NOT NULL DEFAULT '["libretranslate", "argos", "agent"]', google_endpoint TEXT NOT NULL DEFAULT 'https://translation.googleapis.com/language/translate/v2', google_api_key TEXT NOT NULL DEFAULT '', source_language TEXT NOT NULL DEFAULT 'auto', target_language TEXT NOT NULL DEFAULT 'zh-CN', timeout INTEGER NOT NULL DEFAULT 20, cache_enabled INTEGER NOT NULL DEFAULT 1, glossary_id TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS translation_cache (cache_key TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, engine TEXT NOT NULL, translated TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);
        """)
        if db.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0:
            db.executemany("INSERT INTO tags(id,tag,translation,category,source) VALUES(?,?,?,?,?)", [(str(uuid.uuid4()), *row, "seed") for row in SEED_TAGS])
        if db.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0:
            skills = [("anima-tags", "Anima tag 生成", "将自然语言整理为 Anima 英文 tags", "Return concise comma-separated Anima tags. Preserve LoRA tokens and do not invent trigger words.", 1, 0), ("prompt-check", "冲突检查", "检查重复、冲突和缺失项", "Review the prompt for duplicates, contradictory tags, and missing essentials. Return suggestions only.", 1, 1), ("translation", "翻译辅助", "生成中文辅助说明", "Translate explanations to Chinese while preserving all special prompt syntax.", 1, 2)]
            db.executemany("INSERT INTO skills VALUES(?,?,?,?,?,?)", skills)
        if db.execute("SELECT COUNT(*) FROM agent_profiles").fetchone()[0] == 0:
            stamp = now()
            schema = json.dumps({"type": "object", "required": ["variants"], "properties": {"variants": {"type": "array"}}}, ensure_ascii=False)
            agents = [
                ("anima-creator", "Anima Creator", "自然语言生成 Anima 提示词", "You create Anima-compatible prompts from Chinese user intent. Return JSON only.", "calm, precise, visually literate prompt director", schema, "", "", 0.7, 1600, "conversation", 1, stamp, stamp),
                ("anima-reviewer", "Anima Reviewer", "检查冲突、缺失和未知 tag", "Review the supplied prompt for conflicts and missing details. Return JSON suggestions only.", "meticulous Anima prompt editor", schema, "", "", 0.2, 1200, "prompt", 1, stamp, stamp),
                ("translator", "Translation Agent", "翻译自然语言和解释内容", "Translate explanations while preserving protected prompt syntax. Return JSON only.", "faithful technical translator", schema, "", "", 0.2, 1000, "prompt", 1, stamp, stamp),
                ("prompt-explainer", "Prompt Explainer", "解释 tag 和规范化变更", "Explain prompt tags and normalization changes in concise Chinese JSON.", "patient prompt teacher", schema, "", "", 0.2, 1000, "prompt", 1, stamp, stamp),
            ]
            db.executemany("INSERT INTO agent_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", agents)
        if db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            stamp = now()
            defaults = [
                ("ai", "defaults", {"default_agent": "anima-creator"}),
                ("ai", "runtime", {"temperature": 0.7, "max_tokens": 1600, "max_variants": 3}),
                ("catalog", "merge", {"strategy": "priority", "custom_priority": 10, "anima_priority": 20, "danbooru_priority": 80}),
                ("anima", "protection", {"protect_lora": True, "protect_embeddings": True, "protect_break": True}),
                ("appearance", "theme", {"theme": "system", "glass_strength": 0.72, "high_contrast": False}),
                ("appearance", "motion", {"motion": "full", "reduce_motion": False}),
            ]
            db.executemany("INSERT INTO settings VALUES(?,?,?,?)", [(s, g, json.dumps(v, ensure_ascii=False), stamp) for s, g, v in defaults])
        db.execute("INSERT OR IGNORE INTO translation_config(id) VALUES(1)")
        if db.execute("SELECT COUNT(*) FROM catalog_sources").fetchone()[0] == 0:
            db.executemany("INSERT INTO catalog_sources VALUES(?,?,?,?,?,?,?,?,?,?)", [
                ("anima-local", "Anima catalog", "anima", "F:\\comfyuishengtu\\anima_webui\\catalog.py", "local", 1, 20, 0, "", "ready"),
                ("anima-tools", "Anima Tools", "anima-tools", "F:\\comfyui\\custom_nodes\\Comfyui-Anima-Tools", "local", 1, 10, 0, "", "not_synced"),
                ("danbooru-snapshot", "Danbooru snapshot", "danbooru", "data/catalog/danbooru.jsonl", "snapshot", 0, 80, 0, "", "not_synced"),
            ])


def row_json(row: sqlite3.Row) -> dict:
    return dict(row)
