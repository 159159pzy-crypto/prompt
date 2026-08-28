const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const REASONING_OPTIONS = [
  { value: 'none', label: '不思考', description: '兼容性最好，不发送 reasoning_effort。' },
  { value: 'minimal', label: '极简', description: '只使用最少推理预算。' },
  { value: 'low', label: '低', description: '适合清晰、约束较少的画面。' },
  { value: 'medium', label: '中', description: '在速度与复杂约束之间保持平衡。' },
  { value: 'high', label: '高', description: '适合多主体和复杂视觉关系。' },
  { value: 'xhigh', label: '极高', description: '使用路由支持的最高推理预算。' },
];

const STAGE_COPY = {
  queued: '已排队，等待本地 Worker',
  planner: '正在解析意图与 Skills',
  generator: '正在调用模型',
  validator: '正在校验数量档位与禁词',
  finalizer: '正在整理候选',
  completed: '生成完成',
  failed: '生成失败',
  cancelled: '已取消',
};

const VIEW_TITLES = {
  outputs: '输出结果',
  model: '模型与路由',
  provider: '供应商',
  reasoning: '思考强度',
  language: '语言与翻译',
  system: '系统提示词',
  skills: 'Skills',
  documents: '文档库',
};

const SKILL_REASON = {
  core: '核心规则，始终注入',
  explicit: '由 $skill-name 显式指定',
  trigger: '匹配意图中的触发词',
  dimension: '按变体维度注入',
  dependency: '被已选 Skill 依赖',
  disabled: '已停用',
};

const state = {
  settings: {
    include_chinese: false,
    provider_id: '',
    model: '',
    reasoning_effort: 'none',
    system_prompt: '',
  },
  providers: [],
  skills: [],
  runs: [],
  conversations: [],
  conversationsTotal: 0,
  conversationQuery: '',
  conversationOffset: 0,
  documents: [],
  inspectorEvents: [],
  eventCursor: 0,
  lastUsage: {},
  lastSkillIds: [],
  productVersion: '',
  schemaVersion: '',
  lintByIndex: {},
  activeConversationId: '',
  variants: [],
  activeRunId: '',
  activeIntent: '',
  runError: '',
  busy: false,
  idempotencyKey: '',
};

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 120000);
  try {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      signal: controller.signal,
      ...options,
    });
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
    if (!response.ok) {
      const detail = typeof data.detail === 'object'
        ? data.detail.message || JSON.stringify(data.detail)
        : data.detail;
      throw new Error(detail || `请求失败 (${response.status})`);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请检查本地服务或供应商状态');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

const escapeHtml = (value = '') => String(value).replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove('show'), 2200);
}

function setServiceStatus(online, text = '') {
  const target = $('#serviceStatus');
  target.className = `online ${online ? '' : 'offline'}`.trim();
  $('span', target).textContent = text || (online ? '本地服务在线' : '本地服务离线');
}

function setRunMessage(message = '', tone = '') {
  const target = $('#runMessage');
  target.hidden = !message;
  $('#runMessageText').textContent = message;
  target.className = `run-message ${tone}`.trim();
}

function serialize(tokens = []) {
  return tokens.map((token) => {
    const raw = String(token?.raw_text || '').trim();
    const weight = Number(token?.weight || 1);
    return weight !== 1 ? `(${raw}:${weight})` : raw;
  }).filter(Boolean).join(', ');
}

function chineseText(variant) {
  const translations = variant?.positive_translations || [];
  return translations.length ? translations.join('，') : '';
}

async function copyText(value, successMessage = '已复制到剪贴板') {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showToast(successMessage);
  } catch {
    const fallback = document.createElement('textarea');
    fallback.value = value;
    fallback.setAttribute('readonly', '');
    fallback.style.position = 'fixed';
    fallback.style.opacity = '0';
    document.body.appendChild(fallback);
    fallback.select();
    const copied = document.execCommand('copy');
    fallback.remove();
    showToast(copied ? successMessage : '复制失败，请手动选择');
  }
}

function currentProvider() {
  return state.providers.find((provider) => provider.enabled && provider.id === state.settings.provider_id)
    || state.providers.find((provider) => provider.enabled)
    || null;
}

function providerModels(provider) {
  return [...new Set([...(provider?.models || []), provider?.model || ''].filter(Boolean))];
}

function formatRelativeTime(value) {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '未知时间';
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} 天前`;
  return new Date(value).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

function formatStage(stage, progress) {
  const label = STAGE_COPY[stage] || stage || 'queued';
  return progress ? `${label} · ${Number(progress)}%` : label;
}

function normalizeRun(run) {
  const result = run.result && typeof run.result === 'object' ? run.result : {};
  return {
    ...result,
    status: run.status,
    error: run.error || result.error || null,
    usage: (run.usage && Object.keys(run.usage).length ? run.usage : null) || result.usage || {},
    stage: run.stage,
    progress: run.progress,
    id: result.id || run.id || run.run_id,
    conversation_id: result.conversation_id || run.conversation_id,
  };
}

function appendInspectorEvents(items, after) {
  for (const event of items || []) state.inspectorEvents.push(event);
  const seq = (items || []).map((event) => Number(event.sequence) || 0);
  return seq.length ? Math.max(after, ...seq) : after;
}

function renderInspector() {
  const target = $('#runInspector');
  if (!target) return;
  const events = state.inspectorEvents;
  const stages = events.filter((event) => event.event_type === 'stage' || event.stage);
  const tools = events.filter((event) => event.event_type === 'tool_call');
  if (!events.length && !state.busy) {
    target.hidden = false;
    target.innerHTML = '<p class="inspector-empty">生成后将在这里显示阶段、工具调用和用量</p>';
    return;
  }
  if (!events.length && state.busy) {
    target.hidden = false;
    target.innerHTML = '<p class="inspector-empty">正在等待阶段事件…</p>';
    return;
  }
  const stageLine = stages.map((event) => escapeHtml(STAGE_COPY[event.stage] || event.stage || event.event_type)).join(' → ')
    || (state.variants.length ? '此记录没有阶段事件（旧版兼容生成）' : '尚无阶段事件');
  const toolLine = tools.length
    ? tools.map((event) => escapeHtml(event.tool_name || 'tool')).join(' · ')
    : '无工具调用';
  const usage = state.lastUsage || {};
  const usageLine = [usage.latency_ms != null ? `${usage.latency_ms} ms` : '', usage.input_tokens != null ? `入 ${usage.input_tokens}` : '', usage.output_tokens != null ? `出 ${usage.output_tokens}` : ''].filter(Boolean).join(' · ') || '暂无用量';
  const skillLine = (state.lastSkillIds || []).join(', ') || '—';
  target.hidden = false;
  target.innerHTML = `<div class="inspector-row"><strong>阶段</strong><span>${stageLine}</span></div><div class="inspector-row"><strong>工具</strong><span>${toolLine}</span></div><div class="inspector-row"><strong>用量</strong><span>${escapeHtml(usageLine)}</span></div><div class="inspector-row"><strong>Skills</strong><span>${escapeHtml(skillLine)}</span></div>`;
}

function renderRuns() {
  const target = $('#conversationList');
  if (!state.conversations.length) {
    target.innerHTML = '<div class="sidebar-empty">还没有生成记录。</div>';
    $('#loadMoreConversations').hidden = true;
    return;
  }
  target.innerHTML = state.conversations.map((item) => {
    const status = item.latest_status === 'completed'
      ? `${item.variant_count || 0} 个结果 · ${item.revision_count || 1} 个版本`
      : (item.latest_status === 'failed' ? '生成失败' : (item.latest_status || '未完成'));
    const active = item.id === state.activeConversationId ? 'active' : '';
    const pin = item.pinned ? '★ ' : '';
    return `<div class="conversation-row ${active}" data-conversation-id="${escapeHtml(item.id)}">
      <button class="conversation" type="button" data-conversation-id="${escapeHtml(item.id)}">
        <strong>${escapeHtml(pin + (item.title || item.latest_intent || '未命名对话'))}</strong>
        <small>${escapeHtml(formatRelativeTime(item.updated_at))} · ${escapeHtml(status)}</small>
      </button>
      <div class="conversation-actions">
        <button type="button" class="copy-link pin-conversation" data-conversation-id="${escapeHtml(item.id)}" data-pinned="${item.pinned ? '1' : '0'}">${item.pinned ? '取消置顶' : '置顶'}</button>
        <button type="button" class="copy-link rename-conversation" data-conversation-id="${escapeHtml(item.id)}">重命名</button>
        <button type="button" class="copy-link delete-conversation" data-conversation-id="${escapeHtml(item.id)}">删除</button>
      </div>
    </div>`;
  }).join('');
  $('#loadMoreConversations').hidden = state.conversations.length >= state.conversationsTotal;
}

function renderVersions() {
  const select = $('#versionSelect');
  const versions = state.runs;
  select.hidden = !versions.length;
  select.innerHTML = versions.map((run) => `<option value="${escapeHtml(run.id)}">版本 ${Number(run.revision || 1)} · ${escapeHtml(formatRelativeTime(run.created_at))}</option>`).join('');
  if (state.activeRunId) select.value = state.activeRunId;
}

function lintBanner(index) {
  const lint = state.lintByIndex[index];
  if (!lint) return '';
  const band = lint.band;
  const warning = (lint.issues || []).find((issue) => issue.code === 'quantity_out_of_range');
  if (!band && !warning) return '';
  const actual = band?.actual ?? (state.variants[index]?.positive_tokens || []).length;
  if (warning || (band && (actual < band.minimum || actual > band.maximum))) {
    return `<p class="lint-banner">当前 ${actual} 个正面 Token，${escapeHtml(band?.label || '')} 场景建议 ${band?.minimum ?? '?'}–${band?.maximum ?? '?'}。保存可以；再生成会被拒绝。</p>`;
  }
  return '';
}

function renderResults() {
  const target = $('#resultGrid');
  const count = state.variants.length;
  $('#resultEyebrowCount').textContent = String(count).padStart(2, '0');
  $('#copyAll').hidden = !count;
  if (state.busy) {
    $('#resultMeta').textContent = 'Agent 正在生成正面 Prompt';
    target.innerHTML = '<div class="empty-state loading-state"><strong>正在生成</strong><span>请求可能需要一些时间，请保持页面打开。</span></div>';
    return;
  }
  if (!count) {
    $('#resultMeta').textContent = state.runError || '等待输入画面描述';
    target.innerHTML = `<div class="empty-state"><strong>${state.runError ? '没有生成结果' : '等待生成'}</strong><span>${escapeHtml(state.runError || '真实结果会显示在这里。')}</span></div>`;
    return;
  }
  $('#resultMeta').textContent = `已生成 ${count} 个视觉方向 · 仅正面 Prompt`;
  target.innerHTML = state.variants.map((variant, index) => {
    const prompt = serialize(variant.positive_tokens);
    const translation = chineseText(variant);
    const translationBlock = state.settings.include_chinese
      ? `<div class="box-label translation-label"><span>中文逐项对照</span><button class="copy-link copy-translation" type="button" data-result-index="${index}">复制</button></div><div class="output-box cn">${escapeHtml(translation || '该记录没有中文对照')}</div>`
      : '';
    return `<article class="result-card"><header><h3>${escapeHtml(variant.title || `候选 ${index + 1}`)}</h3><span class="result-number">${String(index + 1).padStart(2, '0')} / ${String(count).padStart(2, '0')}</span></header>${lintBanner(index)}<div class="box-label"><span>ENGLISH POSITIVE PROMPT</span><button class="copy-link copy-result" type="button" data-result-index="${index}">复制</button></div><div class="output-box">${escapeHtml(prompt || '模型未返回正面 Token')}</div>${translationBlock}<div class="card-actions"><button class="copy-link save-document" type="button" data-result-index="${index}">保存为文档</button>${count > 1 && index === 0 ? '<button class="copy-link save-all-documents" type="button">保存全部候选</button>' : ''}</div></article>`;
  }).join('');
}

function renderRouteControls() {
  const enabled = state.providers.filter((provider) => provider.enabled);
  const provider = currentProvider();
  if (provider && provider.id !== state.settings.provider_id) state.settings.provider_id = provider.id;
  if (!provider) state.settings.provider_id = '';

  $('#routeProvider').innerHTML = enabled.length
    ? enabled.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === provider?.id ? 'selected' : ''}>${escapeHtml(item.name)}</option>`).join('')
    : '<option value="">没有启用的供应商</option>';
  $('#routeProvider').disabled = !enabled.length;

  const models = providerModels(provider);
  if (!models.includes(state.settings.model)) state.settings.model = provider?.model || models[0] || '';
  $('#routeModel').innerHTML = models.length
    ? models.map((model) => `<option value="${escapeHtml(model)}" ${model === state.settings.model ? 'selected' : ''}>${escapeHtml(model)}</option>`).join('')
    : '<option value="">没有可用模型</option>';
  $('#routeModel').disabled = !models.length;

  $('#routeReasoning').innerHTML = REASONING_OPTIONS.map((option) => `<option value="${option.value}">${option.label}</option>`).join('');
  $('#routeReasoning').value = state.settings.reasoning_effort || 'none';
  $('#generateButton').disabled = state.busy || !provider || !state.settings.model;
}

function providerCard(provider) {
  return `<form class="provider-card" data-provider-id="${escapeHtml(provider.id)}">
    <label class="setting-row"><span><strong>名称</strong></span><input class="select" name="name" value="${escapeHtml(provider.name)}"></label>
    <label class="setting-row"><span><strong>API 地址</strong></span><input class="select" name="base_url" value="${escapeHtml(provider.base_url)}"></label>
    <label class="setting-row"><span><strong>默认模型</strong></span><input class="select" name="model" value="${escapeHtml(provider.model || '')}"></label>
    <label class="setting-row"><span><strong>完成 Token 上限</strong></span><input class="select" name="max_tokens" type="number" min="256" max="100000" value="${escapeHtml(provider.max_tokens || 4096)}"></label>
    <label class="setting-row"><span><strong>超时（秒）</strong></span><input class="select" name="timeout" type="number" min="1" max="300" value="${escapeHtml(provider.timeout || 120)}"></label>
    <label class="setting-row"><span><strong>温度</strong></span><input class="select" name="temperature" type="number" min="0" max="2" step="0.1" value="${escapeHtml(provider.temperature ?? 0.7)}"></label>
    <label class="setting-row"><span><strong>API key</strong><small>${provider.has_api_key ? '已保存' : '未配置'}</small></span><input class="select" name="api_key" type="password" autocomplete="off" placeholder="${provider.has_api_key ? '已保存' : '粘贴新密钥'}"></label>
    <label class="setting-row"><span><strong>环境变量名</strong></span><input class="select" name="env_name" value="${escapeHtml(provider.env_name || '')}"></label>
    <div class="setting-row"><span><strong>启用</strong></span><button class="toggle provider-toggle ${provider.enabled ? 'on' : ''}" type="button" role="switch" aria-checked="${Boolean(provider.enabled)}" data-provider-id="${escapeHtml(provider.id)}"></button></div>
    <div class="settings-actions">
      <button class="copy-link save-provider" type="submit">保存</button>
      <button class="copy-link test-provider" type="button" data-provider-id="${escapeHtml(provider.id)}">测通</button>
      <button class="copy-link sync-provider" type="button" data-provider-id="${escapeHtml(provider.id)}">同步模型</button>
      <button class="copy-link delete-provider" type="button" data-provider-id="${escapeHtml(provider.id)}">删除</button>
    </div>
  </form>`;
}

function renderProviders() {
  const target = $('#providerRows');
  if (!state.providers.length) {
    target.innerHTML = '<div class="settings-empty">尚未配置供应商。请在此页添加连接，或导入 JSON。</div>';
    return;
  }
  target.innerHTML = state.providers.map(providerCard).join('');
}

function renderReasoning() {
  const current = state.settings.reasoning_effort || 'none';
  $('#reasoningRows').innerHTML = REASONING_OPTIONS.map((option) => `<button class="setting-row reasoning-choice" type="button" role="radio" aria-checked="${option.value === current}" data-reasoning="${option.value}"><span><strong>${option.label}</strong><small>${option.description}</small></span><span class="toggle ${option.value === current ? 'on' : ''}" aria-hidden="true"></span></button>`).join('');
}

function renderLanguage() {
  const enabled = Boolean(state.settings.include_chinese);
  $('#includeChineseToggle').classList.toggle('on', enabled);
  $('#includeChineseToggle').setAttribute('aria-checked', String(enabled));
  $('#outputLanguage').value = enabled ? 'bilingual' : 'english';
}

function renderSystemPrompt() {
  const input = $('#systemPromptInput');
  if (input && document.activeElement !== input) input.value = state.settings.system_prompt || '';
}

function renderSkills() {
  const target = $('#skillsRows');
  if (!state.skills.length) {
    target.innerHTML = '<div class="settings-empty">没有可用 Skills。</div>';
    return;
  }
  target.innerHTML = state.skills.map((skill) => {
    const enabled = skill.enabled !== false;
    const core = skill.core === true;
    const reason = SKILL_REASON[skill.selection_reason] || '';
    const note = core ? '核心规则，始终注入' : escapeHtml(reason || skill.description || '');
    return `<div class="setting-row"><span><strong>${escapeHtml(skill.name)}</strong><small>${note}</small></span><button class="toggle skill-toggle ${enabled ? 'on' : ''}" type="button" role="switch" aria-checked="${enabled}" aria-label="${core ? '核心规则不可关闭' : (enabled ? '停用' : '启用') + ' ' + escapeHtml(skill.name)}" data-skill-id="${escapeHtml(skill.id)}" ${core ? 'disabled' : ''}></button></div>`;
  }).join('');
  const mode = $('#skillMode');
  if (mode) { mode.value = 'agent'; mode.disabled = true; }
}

function renderDocuments() {
  const target = $('#documentRows');
  if (!target) return;
  if (!state.documents.length) {
    target.innerHTML = '<div class="settings-empty">还没有保存的文档。在结果卡上点「保存为文档」。</div>';
    return;
  }
  target.innerHTML = state.documents.map((doc) => `<div class="setting-row document-row"><span><strong>${escapeHtml(doc.title)}</strong><small>${escapeHtml(doc.intent || '无意图')} · ${escapeHtml(formatRelativeTime(doc.updated_at))}</small></span><span class="document-actions"><button class="copy-link restore-document" type="button" data-document-id="${escapeHtml(doc.id)}">从文档恢复为修改基线</button><button class="copy-link export-document" type="button" data-document-id="${escapeHtml(doc.id)}">导出</button><button class="copy-link delete-document" type="button" data-document-id="${escapeHtml(doc.id)}">删除</button></span></div>`).join('');
}

function renderAll() {
  renderRuns();
  renderVersions();
  renderResults();
  renderInspector();
  renderRouteControls();
  renderProviders();
  renderReasoning();
  renderLanguage();
  renderSystemPrompt();
  renderSkills();
  renderDocuments();
  $('#conversationTitle').textContent = state.activeIntent || '新对话';
}

function applyWorkspace(workspace) {
  state.settings = {
    ...state.settings,
    ...(workspace.runtime || {}),
  };
  state.providers = workspace.providers || [];
  state.skills = workspace.skills || [];
  state.productVersion = workspace.status?.version || state.productVersion;
  state.schemaVersion = workspace.status?.schema_version || state.schemaVersion;
  const provider = currentProvider();
  state.settings.provider_id = provider?.id || '';
  if (!providerModels(provider).includes(state.settings.model)) {
    state.settings.model = provider?.model || providerModels(provider)[0] || '';
  }
}

function closeWorkspaceMenu() {
  $('#workspaceMenu').hidden = true;
  $('#moreActions').setAttribute('aria-expanded', 'false');
}

async function refreshConversations(reset = true) {
  if (reset) state.conversationOffset = 0;
  const query = encodeURIComponent(state.conversationQuery || '');
  const data = await api(`/api/conversations?q=${query}&limit=20&offset=${state.conversationOffset}`);
  const items = data.items || [];
  state.conversations = reset ? items : [...state.conversations, ...items];
  state.conversationsTotal = data.total || items.length;
  renderRuns();
}

async function loadConversationRuns(conversationId) {
  if (!conversationId) {
    state.runs = [];
    renderVersions();
    return [];
  }
  const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/runs`);
  state.runs = data.items || [];
  renderVersions();
  return state.runs;
}

async function loadDocuments() {
  const data = await api('/api/documents');
  state.documents = data.items || [];
  renderDocuments();
}

async function refreshWorkspace(selectLatest = false) {
  const activeRunId = state.activeRunId;
  const workspace = await api('/api/workspace?limit=20');
  applyWorkspace(workspace);
  setServiceStatus(true);
  await refreshConversations(true);
  if (state.activeConversationId) await loadConversationRuns(state.activeConversationId);
  await loadDocuments();
  renderAll();
  if (selectLatest && state.conversations.length) {
    const latest = state.conversations[0];
    if (latest.latest_run_id) await selectRun(latest.latest_run_id, latest.id);
  } else if (activeRunId) {
    await selectRun(activeRunId, state.activeConversationId);
  }
}

async function persistRuntime(patch) {
  const result = await api('/api/settings/runtime', {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
  state.settings = { ...state.settings, ...(result.payload || {}) };
}

async function loadInspector(runId) {
  state.inspectorEvents = [];
  if (!runId) {
    renderInspector();
    return;
  }
  const events = await api(`/api/runs/${encodeURIComponent(runId)}/events?after=0`);
  appendInspectorEvents(events.items, 0);
  renderInspector();
}

async function selectRun(runId, conversationId = '') {
  const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
  const normalized = normalizeRun(run);
  state.activeRunId = normalized.id || runId;
  state.activeConversationId = conversationId || normalized.conversation_id || '';
  state.activeIntent = normalized.intent || run.result?.intent || state.activeIntent;
  if (state.activeConversationId) {
    const current = state.conversations.find((item) => item.id === state.activeConversationId);
    if (current?.title) state.activeIntent = current.title_source === 'user' ? state.activeIntent : (current.latest_intent || current.title);
    if (current?.latest_intent) state.activeIntent = current.latest_intent;
    await loadConversationRuns(state.activeConversationId);
  }
  state.variants = normalized.variants || [];
  state.lastUsage = normalized.usage || {};
  state.lastSkillIds = normalized.selected_skill_ids || [];
  state.runError = normalized.error?.message || '';
  $('#intentInput').value = '';
  $('#intentInput').placeholder = '输入对当前 Prompt 的修改要求，例如：把头发改成银色，其他内容保持不变';
  setRunMessage(state.runError, state.runError ? 'error' : '');
  await loadInspector(runId);
  if (!state.inspectorEvents.length && normalized.status === 'completed') {
    $('#runInspector').innerHTML = '<p class="inspector-empty">此记录没有阶段事件（旧版兼容生成）</p>';
  }
  showView('outputs');
  renderRuns();
  renderVersions();
  renderResults();
  $('#conversationTitle').textContent = state.activeIntent || '新对话';
  lintVariants();
}

function newChat() {
  state.activeRunId = '';
  state.activeConversationId = '';
  state.activeIntent = '';
  state.variants = [];
  state.runError = '';
  state.inspectorEvents = [];
  state.lintByIndex = {};
  $('#intentInput').value = '';
  setRunMessage();
  showView('outputs');
  renderRuns();
  renderVersions();
  renderResults();
  renderInspector();
  $('#conversationTitle').textContent = '新对话';
  $('#intentInput').placeholder = '例如：生成 5 组服装变体：雨夜的东京街头，一个穿校服的女孩撑伞站在霓虹灯下，电影感构图';
  $('#intentInput').focus();
}

function showView(view) {
  $$('[data-view]').forEach((item) => item.classList.toggle('selected', item.dataset.view === view));
  $$('[data-view-panel]').forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
  $('#workspaceTitle').textContent = VIEW_TITLES[view] || 'Prompt Workbench';
  $('#content').scrollTop = 0;
}

async function waitForRun(runId) {
  let delay = 350;
  let after = 0;
  const waitStarted = Date.now();
  let workerHintShown = false;
  state.inspectorEvents = [];
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`, { timeout: 10000 });
    const events = await api(`/api/runs/${encodeURIComponent(runId)}/events?after=${after}`, { timeout: 10000 });
    after = appendInspectorEvents(events.items, after);
    renderInspector();
    $('#cancelRun').hidden = !['queued', 'running'].includes(run.status);
    $('#retryRun').hidden = !['failed', 'cancelled'].includes(run.status);
    const hasStage = state.inspectorEvents.some((event) => event.event_type === 'stage' || event.stage);
    if (run.status === 'queued' && Date.now() - waitStarted > 5000 && !hasStage) {
      if (!workerHintShown) {
        setRunMessage('本地 Worker 未启动，请运行 run.ps1 或托盘启动器');
        workerHintShown = true;
      }
    } else {
      setRunMessage(formatStage(run.stage, run.progress));
    }
    if (['completed', 'failed', 'cancelled'].includes(run.status)) {
      const normalized = normalizeRun(run);
      state.lastUsage = normalized.usage || {};
      state.lastSkillIds = normalized.selected_skill_ids || [];
      return normalized;
    }
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(2500, Math.round(delay * 1.12));
  }
  throw new Error('Run 轮询超时，请稍后从历史记录恢复');
}

async function lintVariants() {
  state.lintByIndex = {};
  await Promise.all(state.variants.map(async (variant, index) => {
    try {
      state.lintByIndex[index] = await api('/api/documents/lint', {
        method: 'POST',
        body: JSON.stringify({
          intent: variant.intent || state.activeIntent,
          positive_tokens: variant.positive_tokens || [],
        }),
      });
    } catch {
      state.lintByIndex[index] = { issues: [], band: null };
    }
  }));
  renderResults();
}

async function generate() {
  const intent = $('#intentInput').value.trim();
  if (!intent) {
    showToast('请先描述你想生成的画面');
    $('#intentInput').focus();
    return;
  }
  const provider = currentProvider();
  if (!provider || !state.settings.model) {
    showToast('没有可用供应商或模型');
    showView('model');
    return;
  }

  state.busy = true;
  const modifying = Boolean(state.activeConversationId && state.activeRunId && state.variants.length);
  state.idempotencyKey = state.idempotencyKey || `${modifying ? state.activeRunId : 'new'}:${Date.now()}`;
  const originalIntent = state.activeIntent || intent;
  const baseVariants = modifying ? state.variants : [];
  if (!modifying) state.activeIntent = intent;
  state.variants = [];
  state.runError = '';
  state.inspectorEvents = [];
  setRunMessage('正在请求模型，请保持页面打开。');
  renderResults();
  renderInspector();
  renderRouteControls();
  $('#conversationTitle').textContent = modifying ? originalIntent : intent;

  try {
    const created = await api('/api/runs', {
      method: 'POST',
      body: JSON.stringify({
        intent,
        mode: modifying ? 'modify' : 'create',
        conversation_id: modifying ? state.activeConversationId : '',
        parent_run_id: modifying ? state.activeRunId : '',
        current_document: (modifying || baseVariants.length)
          ? { original_intent: originalIntent, variants: baseVariants }
          : {},
        include_chinese: Boolean(state.settings.include_chinese),
        provider_id: provider.id,
        model: state.settings.model,
        reasoning_effort: state.settings.reasoning_effort || 'none',
        idempotency_key: state.idempotencyKey,
      }),
    });
    state.activeRunId = created.run_id || created.id || '';
    const result = await waitForRun(state.activeRunId);
    if (result.status === 'completed') {
      state.activeRunId = result.id || '';
      state.activeConversationId = result.conversation_id || state.activeConversationId || result.id || '';
      state.activeIntent = originalIntent;
      state.variants = result.variants || [];
      $('#intentInput').value = '';
      $('#intentInput').placeholder = '输入对当前 Prompt 的修改要求，例如：把头发改成银色，其他内容保持不变';
    } else if (!modifying) {
      state.activeRunId = result.id || '';
      state.activeConversationId = result.conversation_id || result.id || '';
      state.variants = [];
    } else {
      state.variants = baseVariants;
    }
    state.runError = result.error?.message || '';
    if (result.status === 'completed') {
      setRunMessage(`生成完成 · ${result.model || ''} · ${state.variants.length} 个结果`);
      showToast('生成完成');
      await lintVariants();
    } else {
      setRunMessage(state.runError || '生成失败', 'error');
    }
    await refreshConversations(true);
    if (state.activeConversationId) await loadConversationRuns(state.activeConversationId);
    state.idempotencyKey = '';
  } catch (error) {
    state.runError = error.message;
    setRunMessage(error.message, 'error');
    showToast(error.message);
  } finally {
    state.busy = false;
    $('#cancelRun').hidden = true;
    renderResults();
    renderRouteControls();
  }
}

async function changeRoute(patch) {
  try {
    await persistRuntime(patch);
    renderRouteControls();
    renderReasoning();
    showToast('模型路由已保存');
  } catch (error) {
    showToast(error.message);
  }
}

function formProviderBody(form, extra = {}) {
  const data = new FormData(form);
  return {
    name: String(data.get('name') || '').trim(),
    base_url: String(data.get('base_url') || '').trim(),
    model: String(data.get('model') || '').trim(),
    api_key: String(data.get('api_key') || ''),
    env_name: String(data.get('env_name') || '').trim(),
    temperature: Number(data.get('temperature') || 0.7),
    max_tokens: Number(data.get('max_tokens') || 4096),
    timeout: Number(data.get('timeout') || 120),
    enabled: extra.enabled ?? true,
  };
}

async function toggleProvider(providerId, button) {
  const provider = state.providers.find((item) => item.id === providerId);
  if (!provider) return;
  button.disabled = true;
  try {
    const updated = await api(`/api/providers/${provider.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        name: provider.name,
        base_url: provider.base_url,
        model: provider.model,
        env_name: provider.env_name || '',
        api_key: '',
        temperature: provider.temperature,
        max_tokens: provider.max_tokens,
        timeout: provider.timeout,
        enabled: !provider.enabled,
      }),
    });
    state.providers = state.providers.map((item) => item.id === updated.id ? updated : item);
    const selected = currentProvider();
    state.settings.provider_id = selected?.id || '';
    state.settings.model = selected?.model || providerModels(selected)[0] || '';
    await persistRuntime({ provider_id: state.settings.provider_id, model: state.settings.model });
    renderProviders();
    renderRouteControls();
    showToast(updated.enabled ? '供应商已启用' : '供应商已停用');
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
}

async function setChinese(enabled) {
  try {
    await persistRuntime({ include_chinese: enabled });
    state.settings.include_chinese = enabled;
    renderLanguage();
    renderResults();
    showToast(enabled ? '已开启中文对照' : '已切换为仅英文');
  } catch (error) {
    showToast(error.message);
  }
}

async function toggleSkill(skillId, button) {
  const skill = state.skills.find((item) => item.id === skillId);
  if (!skill || skill.core) return;
  button.disabled = true;
  try {
    const updated = await api(`/api/skills/${skillId}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: !skill.enabled }),
    });
    state.skills = state.skills.map((item) => item.id === skillId ? { ...item, ...updated } : item);
    renderSkills();
    showToast(updated.enabled ? 'Skill 已启用' : 'Skill 已停用');
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
}

async function saveVariantDocument(index) {
  const variant = state.variants[index];
  if (!variant) return;
  const created = await api('/api/documents', {
    method: 'POST',
    body: JSON.stringify({
      title: variant.title || `候选 ${index + 1}`,
      intent: variant.intent || state.activeIntent,
      positive_tokens: variant.positive_tokens || [],
      protected_tokens: variant.protected_tokens || [],
      source_run_id: state.activeRunId,
      conversation_id: state.activeConversationId,
      variant_index: index,
    }),
  });
  await loadDocuments();
  showToast(`已保存「${created.title}」`);
}

async function restoreDocumentAsModifyBase(documentId) {
  const doc = await api(`/api/documents/${encodeURIComponent(documentId)}`);
  state.activeIntent = doc.intent || state.activeIntent;
  if (doc.conversation_id) {
    state.activeConversationId = doc.conversation_id;
    await loadConversationRuns(doc.conversation_id);
    const latest = state.runs[state.runs.length - 1];
    state.activeRunId = doc.source_run_id || latest?.id || '';
  } else if (doc.source_run_id) {
    state.activeRunId = doc.source_run_id;
  }
  state.variants = [{
    title: doc.title,
    intent: doc.intent,
    positive_tokens: doc.positive_tokens,
    positive_translations: (doc.positive_tokens || []).map((token) => token.translation || ''),
    protected_tokens: doc.protected_tokens,
  }];
  state.runError = '';
  $('#intentInput').value = '';
  $('#intentInput').placeholder = '输入对当前 Prompt 的修改要求，例如：把头发改成银色，其他内容保持不变';
  showView('outputs');
  renderResults();
  showToast('已恢复为修改基线，输入修改要求后生成');
  lintVariants();
}

function bindEvents() {
  $('#moreActions').addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = $('#workspaceMenu');
    menu.hidden = !menu.hidden;
    $('#moreActions').setAttribute('aria-expanded', String(!menu.hidden));
  });
  $('#workspaceMenu').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', closeWorkspaceMenu);
  $('#refreshWorkspace').addEventListener('click', async () => {
    closeWorkspaceMenu();
    try { await refreshWorkspace(false); showToast('工作区已刷新'); }
    catch (error) { setServiceStatus(false); showToast(error.message); }
  });
  $('#diagnoseService').addEventListener('click', async () => {
    closeWorkspaceMenu();
    try {
      const result = await api('/api/status', { timeout: 5000 });
      setServiceStatus(true);
      showToast(`服务正常 · v${result.version} · schema v${result.schema_version} · ${result.enabled_providers} 个供应商`);
    } catch (error) {
      setServiceStatus(false);
      showToast(error.message);
    }
  });
  $('#newChat').addEventListener('click', newChat);
  $('#generateButton').addEventListener('click', generate);
  $('#skillsRows').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-id]');
    if (button) toggleSkill(button.dataset.skillId, button);
  });
  $('#cancelRun').addEventListener('click', async () => {
    if (!state.activeRunId) return;
    try { await api(`/api/runs/${encodeURIComponent(state.activeRunId)}/cancel`, { method: 'POST' }); showToast('已请求取消'); }
    catch (error) { showToast(error.message); }
  });
  $('#retryRun').addEventListener('click', async () => {
    if (!state.activeRunId) return;
    try {
      const created = await api(`/api/runs/${encodeURIComponent(state.activeRunId)}/retry`, { method: 'POST' });
      state.activeRunId = created.run_id || created.id;
      const result = await waitForRun(state.activeRunId);
      if (result.status === 'completed') {
        state.variants = result.variants || result.result?.variants || [];
        renderResults();
        showToast('重试完成');
        await lintVariants();
      } else showToast(result.error?.message || '重试失败');
      await refreshConversations(true);
      if (state.activeConversationId) await loadConversationRuns(state.activeConversationId);
    } catch (error) { showToast(error.message); }
  });
  $('#intentInput').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') generate();
  });
  $('#copyAll').addEventListener('click', () => copyText(state.variants.map((variant) => serialize(variant.positive_tokens)).join('\n\n'), '已复制全部英文 Prompt'));

  $('#conversationSearch').addEventListener('input', async (event) => {
    state.conversationQuery = event.target.value.trim();
    try { await refreshConversations(true); } catch (error) { showToast(error.message); }
  });
  $('#loadMoreConversations').addEventListener('click', async () => {
    state.conversationOffset = state.conversations.length;
    try { await refreshConversations(false); } catch (error) { showToast(error.message); }
  });
  $('#conversationList').addEventListener('click', async (event) => {
    const del = event.target.closest('.delete-conversation');
    if (del) {
      event.stopPropagation();
      if (!window.confirm('将永久删除该对话下的全部生成记录。已保存的文档不会删除。此操作不可撤销。建议先备份 data/workbench.sqlite3。')) return;
      try {
        await api(`/api/conversations/${encodeURIComponent(del.dataset.conversationId)}`, { method: 'DELETE' });
        if (state.activeConversationId === del.dataset.conversationId) newChat();
        await refreshConversations(true);
        showToast('对话已删除');
      } catch (error) { showToast(error.message); }
      return;
    }
    const rename = event.target.closest('.rename-conversation');
    if (rename) {
      event.stopPropagation();
      const next = window.prompt('新的对话标题');
      if (!next || !next.trim()) return;
      try {
        await api(`/api/conversations/${encodeURIComponent(rename.dataset.conversationId)}`, { method: 'PATCH', body: JSON.stringify({ title: next.trim() }) });
        await refreshConversations(true);
        showToast('已重命名');
      } catch (error) { showToast(error.message); }
      return;
    }
    const pin = event.target.closest('.pin-conversation');
    if (pin) {
      event.stopPropagation();
      try {
        await api(`/api/conversations/${encodeURIComponent(pin.dataset.conversationId)}`, { method: 'PATCH', body: JSON.stringify({ pinned: pin.dataset.pinned !== '1' }) });
        await refreshConversations(true);
      } catch (error) { showToast(error.message); }
      return;
    }
    const button = event.target.closest('[data-conversation-id]');
    if (!button) return;
    const conversation = state.conversations.find((item) => item.id === button.dataset.conversationId);
    if (conversation?.latest_run_id) await selectRun(conversation.latest_run_id, conversation.id);
  });
  $('#versionSelect').addEventListener('change', (event) => selectRun(event.target.value, state.activeConversationId));
  $('#resultGrid').addEventListener('click', async (event) => {
    const promptButton = event.target.closest('.copy-result');
    if (promptButton) copyText(serialize(state.variants[Number(promptButton.dataset.resultIndex)]?.positive_tokens));
    const translationButton = event.target.closest('.copy-translation');
    if (translationButton) copyText(chineseText(state.variants[Number(translationButton.dataset.resultIndex)]), '已复制中文对照');
    const saveOne = event.target.closest('.save-document');
    if (saveOne) {
      try { await saveVariantDocument(Number(saveOne.dataset.resultIndex)); }
      catch (error) { showToast(error.message); }
    }
    const saveAll = event.target.closest('.save-all-documents');
    if (saveAll) {
      try {
        for (let index = 0; index < state.variants.length; index += 1) await saveVariantDocument(index);
      } catch (error) { showToast(error.message); }
    }
  });

  $$('[data-view]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
  $('#settingsTreeRoot').addEventListener('click', () => {
    const root = $('#settingsTreeRoot');
    const expanded = root.getAttribute('aria-expanded') !== 'true';
    root.setAttribute('aria-expanded', String(expanded));
    root.classList.toggle('expanded', expanded);
    $('#settingsTreeGroup').hidden = !expanded;
  });

  $('#routeProvider').addEventListener('change', (event) => {
    state.settings.provider_id = event.target.value;
    const provider = currentProvider();
    state.settings.model = provider?.model || providerModels(provider)[0] || '';
    changeRoute({ provider_id: state.settings.provider_id, model: state.settings.model });
  });
  $('#routeModel').addEventListener('change', (event) => {
    state.settings.model = event.target.value;
    changeRoute({ model: state.settings.model });
  });
  $('#routeReasoning').addEventListener('change', (event) => {
    state.settings.reasoning_effort = event.target.value;
    changeRoute({ reasoning_effort: state.settings.reasoning_effort });
  });

  $('#providerCreate').addEventListener('click', async () => {
    try {
      const created = await api('/api/providers', {
        method: 'POST',
        body: JSON.stringify({
          name: $('#providerName').value.trim(),
          base_url: $('#providerBaseUrl').value.trim(),
          model: $('#providerModel').value.trim(),
          api_key: $('#providerKey').value,
          env_name: $('#providerEnv').value.trim(),
        }),
      });
      $('#providerKey').value = '';
      state.providers = [...state.providers, created];
      renderProviders();
      renderRouteControls();
      showToast('供应商已添加');
    } catch (error) { showToast(error.message); }
  });
  $('#providerImport').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const items = Array.isArray(parsed) ? parsed : parsed.items;
      if (!Array.isArray(items)) throw new Error('JSON 需要 items 数组');
      const imported = await api('/api/providers/import', { method: 'POST', body: JSON.stringify({ items }) });
      state.providers = [...state.providers, ...(imported.items || [])];
      renderProviders();
      renderRouteControls();
      showToast(`已导入 ${imported.items?.length || 0} 个供应商`);
    } catch (error) { showToast(error.message); }
  });
  $('#providerRows').addEventListener('click', async (event) => {
    const toggle = event.target.closest('.provider-toggle');
    if (toggle) {
      await toggleProvider(toggle.dataset.providerId, toggle);
      return;
    }
    const test = event.target.closest('.test-provider');
    if (test) {
      try {
        const result = await api(`/api/providers/${encodeURIComponent(test.dataset.providerId)}/test`, { method: 'POST' });
        showToast(result.ok ? `测通成功 · ${result.model_count || 0} 个模型` : (result.error || '测通失败'));
      } catch (error) { showToast(error.message); }
      return;
    }
    const sync = event.target.closest('.sync-provider');
    if (sync) {
      try {
        await api(`/api/providers/${encodeURIComponent(sync.dataset.providerId)}/models/sync`, { method: 'POST' });
        const listed = await api('/api/providers');
        state.providers = listed.items || [];
        renderProviders();
        renderRouteControls();
        showToast('模型目录已同步');
      } catch (error) { showToast(error.message); }
      return;
    }
    const remove = event.target.closest('.delete-provider');
    if (remove) {
      if (!window.confirm('删除该供应商？密钥引用会一并清除。')) return;
      try {
        await api(`/api/providers/${encodeURIComponent(remove.dataset.providerId)}`, { method: 'DELETE' });
        state.providers = state.providers.filter((item) => item.id !== remove.dataset.providerId);
        renderProviders();
        renderRouteControls();
        showToast('供应商已删除');
      } catch (error) { showToast(error.message); }
    }
  });
  $('#providerRows').addEventListener('submit', async (event) => {
    const form = event.target.closest('.provider-card');
    if (!form) return;
    event.preventDefault();
    const provider = state.providers.find((item) => item.id === form.dataset.providerId);
    try {
      const updated = await api(`/api/providers/${provider.id}`, {
        method: 'PUT',
        body: JSON.stringify(formProviderBody(form, { enabled: provider.enabled })),
      });
      form.querySelector('[name="api_key"]').value = '';
      state.providers = state.providers.map((item) => item.id === updated.id ? updated : item);
      renderProviders();
      renderRouteControls();
      showToast('供应商已保存');
    } catch (error) { showToast(error.message); }
  });
  $('#reasoningRows').addEventListener('click', (event) => {
    const button = event.target.closest('.reasoning-choice');
    if (!button) return;
    state.settings.reasoning_effort = button.dataset.reasoning;
    changeRoute({ reasoning_effort: state.settings.reasoning_effort });
  });
  $('#includeChineseToggle').addEventListener('click', () => setChinese(!state.settings.include_chinese));
  $('#outputLanguage').addEventListener('change', (event) => setChinese(event.target.value === 'bilingual'));
  $('#systemPromptSave').addEventListener('click', async () => {
    const input = $('#systemPromptInput');
    try {
      await persistRuntime({ system_prompt: input.value.slice(0, 12000) });
      renderSystemPrompt();
      showToast('系统提示词已保存');
    } catch (error) { showToast(error.message); }
  });
  $('#systemPromptReset').addEventListener('click', () => {
    $('#systemPromptInput').value = '';
    showToast('已清空自定义提示词，保存后使用默认规则');
  });
  $('#documentRows').addEventListener('click', async (event) => {
    const restore = event.target.closest('.restore-document');
    if (restore) {
      try { await restoreDocumentAsModifyBase(restore.dataset.documentId); }
      catch (error) { showToast(error.message); }
      return;
    }
    const exported = event.target.closest('.export-document');
    if (exported) {
      try {
        const result = await api(`/api/documents/${encodeURIComponent(exported.dataset.documentId)}/export`, { method: 'POST', body: '{}' });
        await copyText(result.positive, '已复制导出的英文 Prompt');
      } catch (error) { showToast(error.message); }
      return;
    }
    const remove = event.target.closest('.delete-document');
    if (remove) {
      try {
        await api(`/api/documents/${encodeURIComponent(remove.dataset.documentId)}`, { method: 'DELETE' });
        await loadDocuments();
        showToast('文档已删除');
      } catch (error) { showToast(error.message); }
    }
  });
}

async function bootstrap() {
  bindEvents();
  try {
    await refreshWorkspace(true);
  } catch (error) {
    setServiceStatus(false);
    state.runError = error.message;
    setRunMessage(error.message, 'error');
    renderAll();
  }
}

bootstrap();
