# Anima Agent Prompt Studio

本地单用户 Anima 图像提示词生成工作区。用户用中文描述画面，模型按自然语言要求返回任意数量的结构化正面 Token，并可按相同位置返回精确中文对照。

## 启动

```powershell
py -3.11 -m pip install -r requirements.txt
./run.ps1
```

打开 `http://127.0.0.1:8191`。首次启动会创建 `data/workbench.sqlite3`。当前版本采用精简 schema，不迁移旧版 Tag/Catalog/翻译数据。

## 工作流

1. 在输出结果页直接输入自然语言意图；可写“生成 5 组服装变体”，未写数量时默认 1 组。
2. 在模型与路由中选择已有 OpenAI-compatible 供应商、模型和思考强度。
3. 在语言设置中决定是否请求逐 Token 中文对照；多组或变体请求会按语义选择并注入相关 Skills，Agent 仍可按需调用只读工具。
4. 生成后可复制单个或全部英文正面 Prompt，并可从最近对话恢复历史结果。

## Skills（codex 格式）

内置技能以 Codex 兼容格式存放于 `.agents/skills/<name>/SKILL.md`：YAML frontmatter 含 `name`、`display_name`、`description`，正文为指令（参考 learn.chatgpt.com/docs/build-skills.md）。Agent 可通过 `list_skills` 和 `read_skill` 按需读取；`validate_prompt` 与 `normalize_prompt` 提供确定性校验和规范化。可向 `.agents/skills` 添加自定义 SKILL.md（重名/缺 SKILL.md 等会在 `GET /api/skills` 的 diagnostics 中报告）。

前端按现有配置工作：供应商页只负责启停已有连接，不提供新增、密钥编辑或导入入口。供应商配置 API 与旧 Prompt Document API 继续保留，供兼容或外部工具使用。

没有可用供应商、模型超时或模型返回非法结构时，系统会明确返回失败，不会把中文原文伪装成英文 Prompt。
思考强度默认关闭以兼容不支持 `reasoning_effort` 的 OpenAI-compatible 路由；支持该参数的模型可在对话栏选择极简、低、中、高或极高。
供应商设置可单独调整完成 Token 上限，新供应商默认 4096。兼容路由把最终 JSON 放入 `reasoning_content` 时，后端也会提取并校验；只有 `finish_reason=length` 时才提示提高上限。

## API

- `POST /api/generate`
- `GET /api/workspace`
- `GET /api/agent-runs`
- `GET /api/agent-runs/{id}/trace`
- `GET /api/skills`
- `PUT /api/skills/{skill_id}`
- `GET/POST/PATCH /api/documents`
- `GET /api/documents/{id}/versions`
- `POST /api/documents/{id}/restore`
- `POST /api/documents/{id}/validate`
- `POST /api/documents/{id}/export`
- `GET/POST/PUT/DELETE /api/providers`
- `POST /api/providers/import`
- `GET /api/providers/{id}/models`
- `POST /api/providers/{id}/models/sync`
- `GET/PUT /api/settings/{key}`
- `GET /api/status`

Agent 使用 OpenAI Chat Completions `tools`/`tool_calls` 协议，单次生成最多 16 轮、总时限 300 秒。工具调用事件保存在 schema v3 的 `agent_events` 表中，并通过 run trace API 查询；前端保持同步生成和原有布局。

`POST /api/generate` 的新候选只返回 `positive_tokens`，中文模式另返回等长同序的 `positive_translations`。旧文档中的 `negative_tokens` 字段不迁移、不删除，文档读取、版本和导出保持兼容。

文档 lint 会检查重复 Token、数量互斥、§13.6 禁令词；`POST /api/generate` 的模型候选在最终校验时强制 `enforce_quantity=true`，单人 16-30、双人 22-38、复杂 30-48；保存接口保持短文档兼容，`POST /api/documents/{id}/validate` 同样检查场景数量档位。

## 验证

```powershell
py -3.11 -m pytest -q
py -3.11 -m compileall -q backend
node --check static/app.js
git diff --check
```
