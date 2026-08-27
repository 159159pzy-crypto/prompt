import json
from contextlib import contextmanager
import json
import pytest

from fastapi.testclient import TestClient

from backend import db
from backend import agent
from backend.app import app
from backend.documents import validate_document
from backend.skills import instructions
from backend import skill_runtime
from backend import run_store, worker


@contextmanager
def client_for(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workbench.sqlite3")
    with TestClient(app) as client:
        yield client


def test_fresh_schema_contains_only_core_tables(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        assert client.get("/api/status").json()["schema_version"] == 4
        tables = {row[0] for row in db.connect().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"schema_meta", "prompt_documents", "prompt_versions", "agent_runs", "agent_events", "providers", "settings"}


def test_generate_without_provider_is_explicit_failure(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        response = client.post("/api/generate", json={"intent": "雨夜街头的女孩"})
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "failed"
        assert body["variants"] == []
        assert body["error"]["code"] == "provider_unavailable"


def test_generate_modify_keeps_conversation_and_versions(tmp_path, monkeypatch):
    captured = []

    async def fake_generate(body, provider, secret, system_prompt, enabled_skills):
        captured.append(body)
        return {
            "status": "completed",
            "engine": "fake",
            "variants": [{"title": "v", "intent": body.intent, "positive_tokens": [{"raw_text": f"token-{index}"} for index in range(16)]}],
            "error": None,
            "latency_ms": 1,
        }

    monkeypatch.setattr("backend.app.generate_agent", fake_generate)
    monkeypatch.setattr("backend.app._provider_secret", lambda _provider: "test-secret")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test"}).json()
        first = client.post("/api/generate", json={"intent": "雨夜东京街头", "provider_id": provider["id"]}).json()
        second = client.post("/api/generate", json={
            "intent": "把头发改成银色，其他内容保持不变",
            "mode": "modify",
            "conversation_id": first["conversation_id"],
            "parent_run_id": first["id"],
            "current_document": {"original_intent": "雨夜东京街头", "variants": first["variants"]},
            "provider_id": provider["id"],
        }).json()

        assert first["revision"] == 1
        assert second["conversation_id"] == first["conversation_id"]
        assert second["parent_run_id"] == first["id"]
        assert second["revision"] == 2
        assert second["mode"] == "modify"
        assert captured[1].intent == "雨夜东京街头"
        assert captured[1].current_document["modification_request"] == "把头发改成银色，其他内容保持不变"
        runs = client.get("/api/agent-runs").json()["items"]
        assert len(runs) == 2
        assert {item["conversation_id"] for item in runs} == {first["conversation_id"]}


def test_runtime_system_prompt_is_forwarded_to_agent(tmp_path, monkeypatch):
    captured = {}

    async def fake_generate(body, provider, secret, system_prompt, enabled_skills):
        captured["system_prompt"] = system_prompt
        captured["enabled_skills"] = enabled_skills
        return {"status": "failed", "engine": "none", "variants": [], "error": {"code": "test", "message": "captured"}, "latency_ms": 0}

    monkeypatch.setattr("backend.app.generate_agent", fake_generate)
    with client_for(tmp_path, monkeypatch) as client:
        client.put("/api/settings/runtime", json={"system_prompt": "只生成夜景，并保持结构化 Token。"})
        response = client.post("/api/generate", json={"intent": "夜景"})
        assert response.status_code == 200
        assert captured["system_prompt"] == "只生成夜景，并保持结构化 Token。"
        assert captured["enabled_skills"]["anima-tags"] is True


def test_semantic_count_is_forwarded_and_relevant_skills_are_selected(tmp_path, monkeypatch):
    captured = {}

    async def fake_generate(body, provider, secret, system_prompt, enabled_skills):
        captured["count"] = body.requested_count
        captured["skills"] = enabled_skills["__selected_skill_ids"]
        return {"status": "completed", "engine": "fake", "variants": [{"title": str(i)} for i in range(body.requested_count)], "error": None, "latency_ms": 1}

    monkeypatch.setattr("backend.app.generate_agent", fake_generate)
    monkeypatch.setattr("backend.app._provider_secret", lambda _provider: "test-secret")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test"}).json()
        result = client.post("/api/generate", json={"intent": "给我生成5组不同服装", "provider_id": provider["id"]}).json()
        assert result["status"] == "completed"
        assert captured["count"] == 5
        assert len(result["variants"]) == 5
        assert "clothing-library" in captured["skills"]
        assert "conflict-check" in captured["skills"]


def test_skills_can_be_listed_and_toggled(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        listed = client.get("/api/skills")
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()["items"]} == {
            "anima-tags", "token-protection", "slot-order", "conflict-check", "assembly-tree",
            "appearance-library", "clothing-library", "pose-library", "expression-library",
            "camera-scene-library", "mood-library", "special-themes", "deepseek-unrestricted",
        }
        updated = client.put("/api/skills/token-protection", json={"enabled": False})
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert next(item for item in client.get("/api/skills").json()["items"] if item["id"] == "token-protection")["enabled"] is False
        assert client.put("/api/skills/unknown", json={"enabled": False}).status_code == 404


def test_workspace_snapshot_matches_agent_studio_frontend(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        snapshot = client.get("/api/workspace?limit=3")
        assert snapshot.status_code == 200
        body = snapshot.json()
        assert body["status"]["ok"] is True
        assert body["runtime"]["requested_count"] == 1
        assert body["providers"] == []
        assert body["recent_runs"] == []
        assert any(item["id"] == "anima-tags" for item in body["skills"])


def test_durable_run_idempotency_single_active_and_cancel(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        first = client.post("/api/runs", json={"intent": "雨夜女孩", "idempotency_key": "same-key"})
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        duplicate = client.post("/api/runs", json={"intent": "雨夜女孩", "idempotency_key": "same-key"})
        assert duplicate.status_code == 202
        assert duplicate.json()["run_id"] == run_id
        conflict = client.post("/api/runs", json={"intent": "另一条", "conversation_id": first.json()["conversation_id"]})
        assert conflict.status_code == 409
        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        status = client.get(f"/api/runs/{run_id}").json()
        assert status["status"] == "cancelled"


def test_run_events_are_incremental(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post("/api/runs", json={"intent": "雨夜女孩", "idempotency_key": "events-key"}).json()
        run_store.append_event(created["run_id"], {"event_type": "stage", "stage": "planner", "status": "completed"})
        run_store.append_event(created["run_id"], {"event_type": "stage", "stage": "generator", "status": "running"})
        all_events = client.get(f"/api/runs/{created['run_id']}/events").json()["items"]
        assert [item["sequence"] for item in all_events] == [1, 2]
        assert [item["stage"] for item in all_events] == ["planner", "generator"]
        assert len(client.get(f"/api/runs/{created['run_id']}/events?after=1").json()["items"]) == 1


@pytest.mark.asyncio
async def test_worker_executes_queued_run_and_persists_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"}).json()
        created = client.post("/api/runs", json={"intent": "雨夜女孩", "provider_id": provider["id"], "model": "test", "idempotency_key": "worker-key"}).json()

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def post(self, *_args, **_kwargs):
                payload = {"choices": [{"message": {"content": json.dumps({"variants": [{"positive_tokens": ["1girl", "rain"]}]}, ensure_ascii=False), "tool_calls": []}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 4}}
                class Response:
                    def raise_for_status(self): return None
                    def json(self): return payload
                return Response()

        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
        claimed = run_store.claim_next("test-worker")
        assert claimed["id"] == created["run_id"]
        await worker.execute_run(claimed, "test-worker")
        status = client.get(f"/api/runs/{created['run_id']}").json()
        assert status["status"] == "completed"
        assert status["result"]["variants"][0]["positive_tokens"][0]["raw_text"] == "1girl"
        assert len(client.get(f"/api/runs/{created['run_id']}/events").json()["items"]) >= 4


def document_payload():
    return {
        "title": "Rainy Tokyo",
        "intent": "雨夜东京街头，一个撑伞的女孩",
        "positive_tokens": [
            {"raw_text": "1girl", "category": "Character"},
            {"raw_text": "<lora:rain:0.8>", "weight": 1, "locked": True},
            {"raw_text": "BREAK"},
        ],
        "negative_tokens": [{"raw_text": "low quality"}],
        "notes": "keep the LoRA trigger locked",
    }


def test_document_round_trip_validation_version_restore_and_export(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post("/api/documents", json=document_payload()).json()
        assert created["protected_tokens"] == ["<lora:rain:0.8>", "BREAK"]
        exported = client.post(f"/api/documents/{created['id']}/export", json={}).json()
        assert "1girl" in exported["positive"]
        assert "<lora:rain:0.8>" in exported["positive"]
        assert "low quality" in exported["negative"]
        assert client.post(f"/api/documents/{created['id']}/validate").json()["valid"] is True

        updated_payload = {**document_payload(), "positive_tokens": [{"raw_text": "1girl"}, {"raw_text": "night"}]}
        updated = client.patch(f"/api/documents/{created['id']}", json=updated_payload).json()
        assert [item["raw_text"] for item in updated["positive_tokens"]] == ["1girl", "night"]
        versions = client.get(f"/api/documents/{created['id']}/versions").json()["items"]
        assert len(versions) == 2
        restored = client.post(f"/api/documents/{created['id']}/restore", json={"version_id": versions[-1]["id"]}).json()
        assert [item["raw_text"] for item in restored["positive_tokens"]] == ["1girl", "<lora:rain:0.8>", "BREAK"]


def test_duplicate_and_conflicting_tokens_are_rejected(tmp_path, monkeypatch):
    with client_for(tmp_path, monkeypatch) as client:
        duplicate = client.post("/api/documents", json={"positive_tokens": [{"raw_text": "1girl"}, {"raw_text": "1girl"}]} )
        assert duplicate.status_code == 422
        valid_pair = {"positive_tokens": [{"raw_text": "1girl"}, {"raw_text": "1boy"}]}
        assert not any(issue["code"] == "conflicting_subject" for issue in validate_document({**valid_pair, "negative_tokens": []}))


def test_backend_lint_checks_section_13_6_and_quantity_band():
    forbidden = {"positive_tokens": [{"raw_text": "sunlight"}], "negative_tokens": []}
    issues = validate_document(forbidden)
    assert any(issue["code"] == "forbidden_section_13_6" for issue in issues)

    pair = {"intent": "two-person foreplay", "positive_tokens": [{"raw_text": "1girl"}, {"raw_text": "1boy"}], "negative_tokens": []}
    issues = validate_document(pair, enforce_quantity=True)
    quantity = next(issue for issue in issues if issue["code"] == "quantity_out_of_range")
    assert quantity["band"] == "standard"
    assert quantity["minimum"] == 22

    solo = {"intent": "单人展示", "positive_tokens": [{"raw_text": "solo"}], "negative_tokens": []}
    solo_issues = validate_document(solo, enforce_quantity=True)
    solo_quantity = next(issue for issue in solo_issues if issue["code"] == "quantity_out_of_range")
    assert solo_quantity["band"] == "simple"
    assert solo_quantity["minimum"] == 16


def test_compact_skill_mode_injects_core_and_relevant_library_only():
    compact = instructions({"__mode": "compact", "__intent": "雨夜街头女孩穿制服"})
    full = instructions({"__mode": "full", "__intent": "雨夜街头女孩穿制服"})
    assert len(compact) < len(full)
    assert any("Anima-compatible tags" in item for item in compact)
    assert any("服装与状态标签库" in item for item in compact)
    assert not any("特殊主题配方" in item for item in compact)


def test_generation_request_parser_supports_chinese_and_english_counts():
    assert agent.parse_generation_request("给我生成5组服装变体")["requested_count"] == 5
    assert agent.parse_generation_request("给我生成五组服装变体")["requested_count"] == 5
    parsed = agent.parse_generation_request("generate 5 prompts for different outfits")
    assert parsed["requested_count"] == 5
    assert parsed["explicit_count"] is True
    assert "clothing-library" in parsed["variation_dimensions"]


def test_generation_request_parser_defaults_to_one_and_detects_dedupe():
    assert agent.parse_generation_request("雨夜女孩")["requested_count"] == 1
    parsed = agent.parse_generation_request("给我多组不同服装")
    assert parsed["requested_count"] == 1
    assert parsed["dedupe_required"] is True
    assert "clothing-library" in parsed["variation_dimensions"]


class FakeResponse:
    def __init__(self, content, reasoning_content="", finish_reason="stop", usage=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.finish_reason = finish_reason
        self.usage = usage or {"prompt_tokens": 4, "completion_tokens": 8}

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content, "reasoning_content": self.reasoning_content}, "finish_reason": self.finish_reason}], "usage": self.usage}


class FakeClient:
    requests = []

    def __init__(self, content, reasoning_content="", finish_reason="stop", usage=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.finish_reason = finish_reason
        self.usage = usage

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        self.requests.append(_kwargs)
        return FakeResponse(self.content, self.reasoning_content, self.finish_reason, self.usage)


class SequenceFakeClient(FakeClient):
    def __init__(self, contents):
        self.contents = iter(contents)
        self.requests = []

    async def post(self, *_args, **_kwargs):
        self.requests.append(_kwargs)
        return FakeResponse(next(self.contents))


def test_model_output_is_validated_and_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"}).json()
        assert provider["timeout"] == 120
        assert provider["max_tokens"] == 4096
        valid = {"variants": [{"title": "Rain", "intent": "雨夜", "positive_tokens": ["1girl", "blue eyes"], "negative": "low quality, bad anatomy"}]}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(json.dumps(valid, ensure_ascii=False)))
        result = client.post("/api/generate", json={"intent": "雨夜女孩"}).json()
        assert result["status"] == "completed"
        variant = result["variants"][0]
        assert variant["positive_tokens"][0]["raw_text"] == "1girl"
        assert "negative_tokens" not in variant
        assert "negative_translations" not in variant

        routed = client.post("/api/generate", json={"intent": "雨夜女孩", "model": "gpt-5.6-sol", "reasoning_effort": "high"}).json()
        assert routed["model"] == "gpt-5.6-sol"
        assert routed["reasoning_effort"] == "high"
        assert FakeClient.requests[-1]["json"]["model"] == "gpt-5.6-sol"
        assert FakeClient.requests[-1]["json"]["reasoning_effort"] == "high"
        assert FakeClient.requests[-1]["json"]["max_completion_tokens"] == 4096
        assert "max_tokens" not in FakeClient.requests[-1]["json"]

        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient("not json"))
        invalid = client.post("/api/generate", json={"intent": "再次生成"}).json()
        assert invalid["status"] == "failed"
        assert invalid["variants"] == []
        assert invalid["error"]["code"] == "provider_json_invalid"


def test_json_in_reasoning_content_is_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "deepseek-v4-flash", "env_name": "ANIMA_TEST_KEY"})
        valid = {"variants": [{"title": "Rain", "positive_tokens": ["1girl", "rain"], "negative_tokens": ["low quality"]}]}
        reasoning = "The requested JSON follows.\n```json\n" + json.dumps(valid) + "\n```"
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient("", reasoning_content=reasoning))

        result = client.post("/api/generate", json={"intent": "雨夜女孩"}).json()

        assert result["status"] == "completed"
        assert [item["raw_text"] for item in result["variants"][0]["positive_tokens"]] == ["1girl", "rain"]


def test_empty_content_reports_actual_token_limit_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY", "max_tokens": 2048})
        usage = {"prompt_tokens": 10, "completion_tokens": 2048}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient("", finish_reason="length", usage=usage))

        result = client.post("/api/generate", json={"intent": "雨夜女孩"}).json()

        assert result["status"] == "failed"
        assert result["error"]["code"] == "provider_empty_content"
        assert "当前上限 2048" in result["error"]["message"]
        assert result["usage"]["output_tokens"] == 2048


def test_malformed_model_json_is_repaired_before_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"})
        malformed = '''```json
        {
          "variants": [{
            "title": "Rain"
            "positive_tokens": ["1girl", "rain"],
            "negative_tokens": ["low quality"]
          }]
        }
        ```'''
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(malformed))

        result = client.post("/api/generate", json={"intent": "雨夜女孩"}).json()

        assert result["status"] == "completed"
        assert [item["raw_text"] for item in result["variants"][0]["positive_tokens"]] == ["1girl", "rain"]


def test_chinese_explanation_is_required_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"})
        valid = {"variants": [{"title": "Rain", "positive_tokens": ["1girl"], "negative_tokens": ["low quality"], "positive_translations": ["一个女孩"], "negative_translations": ["低质量"]}]}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(json.dumps(valid, ensure_ascii=False)))
        result = client.post("/api/generate", json={"intent": "雨夜女孩", "include_chinese": True}).json()
        assert result["status"] == "completed"
        variant = result["variants"][0]
        assert variant["positive_translations"] == ["一个女孩"]
        assert variant["chinese_explanation"] == "一个女孩"
        assert "negative_tokens" not in variant
        assert "negative_translations" not in variant

        missing = {"variants": [{"positive_tokens": ["1girl"]}]}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(json.dumps(missing, ensure_ascii=False)))
        result = client.post("/api/generate", json={"intent": "再次生成", "include_chinese": True}).json()
        assert result["status"] == "failed"
        assert result["variants"] == []
        assert result["error"]["code"] == "token_translation_invalid"

        mismatch = {"variants": [{"positive_tokens": ["1girl", "blue eyes"], "positive_translations": ["一个女孩"]}]}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(json.dumps(mismatch, ensure_ascii=False)))
        result = client.post("/api/generate", json={"intent": "数量不匹配", "include_chinese": True}).json()
        assert result["error"]["code"] == "token_translation_count_mismatch"

        protected = {"variants": [{"positive_tokens": [{"raw_text": "<lora:rain:0.8>", "locked": True}], "positive_translations": ["雨天风格"]}]}
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: FakeClient(json.dumps(protected, ensure_ascii=False)))
        result = client.post("/api/generate", json={"intent": "保护词不能翻译", "include_chinese": True}).json()
        assert result["error"]["code"] == "protected_translation_changed"


def test_invalid_model_fields_are_retried_once_with_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"})
        missing_translations = {"variants": [{"positive_tokens": ["1girl"]}]}
        corrected = {"variants": [{"positive_tokens": ["1girl"], "positive_translations": ["一个女孩"]}]}
        fake_client = SequenceFakeClient([
            json.dumps(missing_translations, ensure_ascii=False),
            json.dumps(corrected, ensure_ascii=False),
        ])
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: fake_client)

        result = client.post("/api/generate", json={"intent": "雨夜女孩", "include_chinese": True}).json()

        assert result["status"] == "completed"
        assert result["variants"][0]["positive_translations"] == ["一个女孩"]
        assert len(fake_client.requests) == 2
        assert "failed validation" in fake_client.requests[-1]["json"]["messages"][-1]["content"]


def test_agent_tool_loop_persists_trace_and_exposes_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"}).json()
        final = {"variants": [{"title": "Rain", "positive_tokens": ["1girl", "rain"]}]}
        class ToolClient:
            def __init__(self): self.calls = 0; self.requests = []
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def post(self, *_args, **kwargs):
                self.requests.append(kwargs)
                self.calls += 1
                if self.calls == 1:
                    content = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "list_skills", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
                else:
                    content = {"choices": [{"message": {"content": json.dumps(final), "tool_calls": []}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}
                class Response:
                    def raise_for_status(self): return None
                    def json(self_nonlocal): return content
                return Response()
        fake = ToolClient()
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: fake)
        result = client.post("/api/generate", json={"intent": "雨夜女孩", "provider_id": provider["id"]}).json()
        assert result["status"] == "completed"
        assert result["tool_trace"][1]["tool_name"] == "list_skills"
        trace = client.get(f"/api/agent-runs/{result['id']}/trace").json()
        assert [item["event_type"] for item in trace["items"]] == ["model_request", "tool_call", "model_request", "final"]
        assert "tools" in fake.requests[0]["json"]


def test_vague_multi_request_uses_selected_model_for_helper_and_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "old-model", "env_name": "ANIMA_TEST_KEY"}).json()
        class MultiClient:
            def __init__(self): self.requests = []
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def post(self, *_args, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    payload = {"choices": [{"message": {"content": '{"requested_count":2,"explicit_count":false,"variation_dimensions":[],"dedupe_required":true}'}, "finish_reason": "stop"}]}
                else:
                    tokens = [f"token-{index}" for index in range(16)]
                    payload = {"choices": [{"message": {"content": json.dumps({"variants": [{"positive_tokens": tokens}, {"positive_tokens": tokens + ["rain"]}]})}, "finish_reason": "stop"}]}
                class Response:
                    def raise_for_status(self): return None
                    def json(self_nonlocal): return payload
                return Response()
        fake = MultiClient()
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: fake)
        result = client.post("/api/generate", json={"intent": "生成多组不同方案", "provider_id": provider["id"], "model": "new-model"}).json()
        assert result["status"] == "completed"
        assert [request["json"]["model"] for request in fake.requests] == ["new-model", "new-model"]


def test_unknown_tool_is_returned_to_agent_as_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_TEST_KEY", "test-only")
    with client_for(tmp_path, monkeypatch) as client:
        provider = client.post("/api/providers", json={"name": "test", "base_url": "http://model.local/v1", "model": "test", "env_name": "ANIMA_TEST_KEY"}).json()
        class UnknownClient:
            def __init__(self): self.calls = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def post(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    payload = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
                else:
                    payload = {"choices": [{"message": {"content": '{"variants":[{"positive_tokens":["1girl"]}]}', "tool_calls": []}, "finish_reason": "stop"}]}
                class Response:
                    def raise_for_status(self): return None
                    def json(self_nonlocal): return payload
                return Response()
        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **_kwargs: UnknownClient())
        result = client.post("/api/generate", json={"intent": "测试", "provider_id": provider["id"]}).json()
        assert result["status"] == "completed"
        assert result["tool_trace"][1]["status"] == "failed"


class FakeModelsResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.6-mini"}, {"id": "gpt-5.6-sol"}]}


class FakeModelsClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return FakeModelsResponse()


def test_multiple_providers_import_and_model_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_ONE_KEY", "one")
    monkeypatch.setenv("ANIMA_TWO_KEY", "two")
    monkeypatch.setattr("backend.app.httpx.AsyncClient", lambda **_kwargs: FakeModelsClient())
    with client_for(tmp_path, monkeypatch) as client:
        imported = client.post("/api/providers/import", json={"items": [
            {"name": "One", "base_url": "https://one.example/v1", "env_name": "ANIMA_ONE_KEY"},
            {"name": "Two", "base_url": "https://two.example/v1", "env_name": "ANIMA_TWO_KEY", "model": "gpt-5.6-sol"},
        ]})
        assert imported.status_code == 200
        assert len(imported.json()["items"]) == 2
        providers = client.get("/api/providers").json()["items"]
        assert len(providers) == 2
        assert providers[0]["models"] == ["gpt-5.6-mini", "gpt-5.6-sol"]
        assert providers[0]["model"] == "gpt-5.6-mini"
        assert providers[1]["model"] == "gpt-5.6-sol"
        assert providers[0]["models_synced_at"]


def test_generation_uses_explicit_provider_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIMA_ONE_KEY", "one")
    monkeypatch.setenv("ANIMA_TWO_KEY", "two")
    captured = {}

    async def fake_generate(body, provider, secret, system_prompt, enabled_skills):
        captured.update(provider_id=provider["id"], model=body.model, effort=body.reasoning_effort, secret=secret)
        return {"status": "completed", "engine": "test", "variants": [{"title": "ok"}], "error": None, "latency_ms": 1}

    monkeypatch.setattr("backend.app.generate_agent", fake_generate)
    with client_for(tmp_path, monkeypatch) as client:
        first = client.post("/api/providers", json={"name": "One", "base_url": "https://one.example/v1", "model": "model-one", "env_name": "ANIMA_ONE_KEY"}).json()
        second = client.post("/api/providers", json={"name": "Two", "base_url": "https://two.example/v1", "model": "model-two", "env_name": "ANIMA_TWO_KEY"}).json()
        response = client.post("/api/generate", json={"intent": "test", "provider_id": second["id"], "model": "model-two-fast", "reasoning_effort": "xhigh"})
        assert response.status_code == 200
        assert captured == {"provider_id": second["id"], "model": "model-two-fast", "effort": "xhigh", "secret": "two"}
        assert captured["provider_id"] != first["id"]


def write_codex_skill(root, name="demo", description="Use demo workflow for rainy scenes.", body="Demo instructions", display_name="Demo"):
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndisplay_name: {display_name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_codex_skills_are_discovered_with_metadata_and_diagnostics(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, body="Only load this body when selected.")
    (tmp_path / ".agents" / "skills" / "missing").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "broken").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "broken" / "SKILL.md").write_text("---\nname: broken\n---", encoding="utf-8")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    items, diagnostics = skill_runtime.catalog({})

    assert items[0]["id"] == "demo"
    assert items[0]["source"] == "codex"
    assert items[0]["name"] == "Demo"
    assert items[0]["path"].endswith(".agents\\skills\\demo\\SKILL.md") or items[0]["path"].endswith(".agents/skills/demo/SKILL.md")
    assert {item["code"] for item in diagnostics} == {"missing_skill_file", "invalid_skill"}


def test_duplicate_skill_name_is_reported(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, name="demo")
    duplicate = tmp_path / ".agents" / "skills" / "demo-copy"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("---\nname: demo\ndescription: Duplicate copy\n---\n\nBody\n", encoding="utf-8")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    items, diagnostics = skill_runtime.catalog({})

    assert [item["id"] for item in items] == ["demo"]
    assert diagnostics[0]["code"] == "duplicate_skill_name"


def test_codex_skill_explicit_trigger_strips_marker_and_unknown_marker_is_preserved(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, description="Rain workflow", body="RAIN BODY")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    activated = skill_runtime.activate("Make a rainy image $demo $missing")

    assert activated["intent"] == "Make a rainy image $missing"
    assert activated["selected_skill_ids"] == ["demo"]
    assert activated["diagnostics"][-1]["code"] == "unknown_skill"


def test_codex_skill_implicit_policy_and_enabled_state(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, description="Rain workflow")
    blocked = write_codex_skill(tmp_path, name="blocked", description="Snow workflow")
    (blocked / "agents").mkdir()
    (blocked / "agents" / "openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    implicit = skill_runtime.activate("rain and snow", {"demo": True, "blocked": True})
    disabled = skill_runtime.activate("rain", {"demo": False})
    explicit = skill_runtime.activate("$blocked", {"blocked": True})

    assert implicit["selected_skill_ids"] == ["demo"]
    assert disabled["selected_skill_ids"] == []
    assert explicit["selected_skill_ids"] == ["blocked"]


def test_codex_skill_body_is_loaded_only_when_selected(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, description="Rain workflow", body="RAIN BODY")
    write_codex_skill(tmp_path, name="other", description="Other workflow", body="OTHER BODY")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    activated = skill_runtime.activate("rain")
    rendered = skill_runtime.render_selected(activated["selected"])

    assert any("RAIN BODY" in item for item in rendered)
    assert all("OTHER BODY" not in item for item in rendered)


def test_injected_skill_read_returns_compact_metadata(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, name="demo", description="Rain workflow", body="RAIN BODY")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)

    result = agent._tool_read_skill({"skill_id": "demo"}, {"demo"})

    assert result["instruction_injected"] is True
    assert "RAIN BODY" not in result["instruction"]
    assert "已注入" in result["instruction"]


def test_codex_skill_api_toggle_and_generation_selection(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, description="Rain workflow", body="RAIN BODY")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)
    captured = {}

    async def fake_generate(body, provider, secret, system_prompt, enabled_skills):
        captured["intent"] = body.intent
        captured["enabled_skills"] = enabled_skills
        return {"status": "failed", "engine": "none", "variants": [], "error": {"code": "test", "message": "captured"}, "latency_ms": 0}

    monkeypatch.setattr("backend.app.generate_agent", fake_generate)
    with client_for(tmp_path, monkeypatch) as client:
        listed = client.get("/api/skills").json()
        assert next(item for item in listed["items"] if item["id"] == "demo")["source"] == "codex"
        toggled = client.put("/api/skills/demo", json={"enabled": False})
        assert toggled.status_code == 200
        assert toggled.json()["enabled"] is False
        client.put("/api/skills/demo", json={"enabled": True})
        response = client.post("/api/generate", json={"intent": "rain scene $demo"})
        assert response.status_code == 200
        assert response.json()["selected_skill_ids"] == ["demo"]
        assert captured["intent"] == "rain scene"
        assert captured["enabled_skills"]["__intent"] == "rain scene"
        assert captured["enabled_skills"]["demo"] is True


def test_explicit_skill_marker_survives_run_queue_and_worker_payload(tmp_path, monkeypatch):
    write_codex_skill(tmp_path, name="special", description="Unrelated trigger", body="SPECIAL BODY")
    monkeypatch.setattr(skill_runtime, "REPO_ROOT", tmp_path)
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post("/api/runs", json={"intent": "普通描述 $special", "idempotency_key": "explicit-run"})
        assert created.status_code == 202
        run = run_store.get_run(created.json()["run_id"])
        assert run["request"]["current_document"]["_explicit_skill_ids"] == ["special"]
        payload = worker._request_body(run["request"])
        assert payload.current_document["_explicit_skill_ids"] == ["special"]


def test_disabled_non_core_skill_is_not_injected_even_when_requested():
    state = {"__mode": "compact", "__intent": "雨夜制服", "clothing-library": False}
    selected = instructions(state)
    assert not any("服装与状态标签库" in item for item in selected)
