# Anima Prompt Workbench

本地单用户 Anima Agent 提示词工作台。核心流程是中文自然语言 -> 多组候选 -> Anima 规范化 -> 可拖拽编辑的 Prompt Document。

## 启动

```powershell
py -3.11 -m pip install -r requirements.txt
./run.ps1
```

浏览器打开 `http://127.0.0.1:8191`，端口可通过 `./run.ps1 -Port 8192` 修改。

## 功能

- 左侧 Agent 对话、中间候选提示词、右侧 Prompt Document 三栏工作区
- Agent 系统提示词、人格、供应商和运行参数集中在设置中心
- 正/负面提示词 chip 与文本双视图，拖拽排序、双击编辑、权重和删除
- Anima-safe parser/serializer，保留括号内逗号、LoRA、embedding 和 `BREAK`
- 固定 Anima 规范化：下划线、score、年份、画师格式、去重和特殊 token 保护
- 本地 Tag 仓库，支持分类、英文/中文搜索、识别、收藏和插入
- SQLite 持久化、收藏、JSON/Markdown/Anima 导出
- OpenAI 兼容供应商配置，以及 Google Cloud Translation 辅助翻译配置
- Apple-inspired 毛玻璃界面、响应式布局和减少动效支持

## 接口

后端入口是 `backend/app.py`，解析与规范化在 `backend/prompt.py`。主要接口包括 `/api/agent-runs`、`/api/agents`、`/api/settings/tree`、`/api/catalog/tags`、`/api/catalog/recognize`、`/api/prompts/normalize`、`/api/translation/config` 和 `/api/prompts/{id}/export`。

API key 不会出现在普通列表响应、导出或日志中。当前本地版本使用 SQLite 保存配置，生产化部署应接入 Windows Credential Manager。

## 验证

```powershell
py -3.11 -m pytest -q
py -3.11 -m compileall -q backend
node --check static/app.js
```
