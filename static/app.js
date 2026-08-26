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

const VIEW_TITLES = {
  outputs: '输出结果',
  model: '模型与路由',
  provider: '供应商',
  reasoning: '思考强度',
  language: '语言与翻译',
  skills: 'Skills',
};

const state = {
  settings: {
    requested_count: 1,
    include_chinese: false,
    provider_id: '',
    model: '',
    reasoning_effort: 'none',
  },
  providers: [],
  skills: [],
  runs: [],
  variants: [],
  activeRunId: '',
  activeIntent: '',
  runError: '',
  busy: false,
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

function clampCount(value) {
  return Math.max(1, Math.min(3, Number(value) || 1));
}

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
  target.textContent = message;
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

function renderRuns() {
  const target = $('#conversationList');
  if (!state.runs.length) {
    target.innerHTML = '<div class="sidebar-empty">还没有生成记录。</div>';
    return;
  }
  target.innerHTML = state.runs.map((run) => {
    const variants = run.response?.variants || [];
    const status = run.status === 'completed' ? `${variants.length} 个生成结果` : '生成失败';
    return `<button class="conversation ${run.id === state.activeRunId ? 'active' : ''}" type="button" data-run-id="${escapeHtml(run.id)}"><strong>${escapeHtml(run.intent || '未命名对话')}</strong><small>${escapeHtml(formatRelativeTime(run.created_at))} · ${escapeHtml(status)}</small></button>`;
  }).join('');
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
    return `<article class="result-card"><header><h3>${escapeHtml(variant.title || `候选 ${index + 1}`)}</h3><span class="result-number">${String(index + 1).padStart(2, '0')} / ${String(count).padStart(2, '0')}</span></header><div class="box-label"><span>ENGLISH POSITIVE PROMPT</span><button class="copy-link copy-result" type="button" data-result-index="${index}">复制</button></div><div class="output-box">${escapeHtml(prompt || '模型未返回正面 Token')}</div>${translationBlock}</article>`;
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
  $('#resultCount').value = String(clampCount(state.settings.requested_count));
  $('#generateButton').disabled = state.busy || !provider || !state.settings.model;
}

function renderProviders() {
  const target = $('#providerRows');
  if (!state.providers.length) {
    target.innerHTML = '<div class="settings-empty">尚未配置供应商。此精简界面不提供新增入口。</div>';
    return;
  }
  target.innerHTML = state.providers.map((provider) => `<div class="setting-row"><span><strong>${escapeHtml(provider.name)}</strong><small>${escapeHtml(provider.base_url)} · ${provider.models?.length || 0} 个模型</small></span><button class="toggle provider-toggle ${provider.enabled ? 'on' : ''}" type="button" role="switch" aria-checked="${Boolean(provider.enabled)}" aria-label="${provider.enabled ? '停用' : '启用'} ${escapeHtml(provider.name)}" data-provider-id="${escapeHtml(provider.id)}"></button></div>`).join('');
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

function renderSkills() {
  const target = $('#skillsRows');
  if (!state.skills.length) {
    target.innerHTML = '<div class="settings-empty">没有可用 Skills。</div>';
    return;
  }
  target.innerHTML = state.skills.map((skill) => `<div class="setting-row"><span><strong>${escapeHtml(skill.name)}</strong><small>${escapeHtml(skill.description)}</small></span><button class="toggle skill-toggle ${skill.enabled ? 'on' : ''}" type="button" role="switch" aria-checked="${Boolean(skill.enabled)}" aria-label="${skill.enabled ? '停用' : '启用'} ${escapeHtml(skill.name)}" data-skill-id="${escapeHtml(skill.id)}"></button></div>`).join('');
  $('#skillMode').value = state.settings.skill_mode || 'compact';
}

function renderAll() {
  renderRuns();
  renderResults();
  renderRouteControls();
  renderProviders();
  renderReasoning();
  renderLanguage();
  renderSkills();
  $('#conversationTitle').textContent = state.activeIntent || '新对话';
}

function applyWorkspace(workspace) {
  state.settings = {
    ...state.settings,
    ...(workspace.runtime || {}),
    requested_count: clampCount(workspace.runtime?.requested_count),
  };
  state.providers = workspace.providers || [];
  state.skills = workspace.skills || [];
  state.runs = workspace.recent_runs || [];
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

async function refreshWorkspace(selectLatest = false) {
  const activeRunId = state.activeRunId;
  const workspace = await api('/api/workspace?limit=20');
  applyWorkspace(workspace);
  setServiceStatus(true);
  renderAll();
  if (selectLatest && state.runs.length) selectRun(state.runs[0].id);
  else if (activeRunId && state.runs.some((run) => run.id === activeRunId)) selectRun(activeRunId);
}

async function persistRuntime(patch) {
  const result = await api('/api/settings/runtime', {
    method: 'PUT',
    body: JSON.stringify(patch),
  });
  state.settings = { ...state.settings, ...(result.payload || {}) };
  state.settings.requested_count = clampCount(state.settings.requested_count);
}

function selectRun(runId) {
  const run = state.runs.find((item) => item.id === runId);
  if (!run) return;
  state.activeRunId = run.id;
  state.activeIntent = run.intent || '';
  state.variants = run.response?.variants || [];
  state.runError = run.response?.error?.message || run.error?.message || '';
  $('#intentInput').value = state.activeIntent;
  setRunMessage(state.runError, state.runError ? 'error' : '');
  showView('outputs');
  renderRuns();
  renderResults();
  $('#conversationTitle').textContent = state.activeIntent || '新对话';
}

function newChat() {
  state.activeRunId = '';
  state.activeIntent = '';
  state.variants = [];
  state.runError = '';
  $('#intentInput').value = '';
  setRunMessage();
  showView('outputs');
  renderRuns();
  renderResults();
  $('#conversationTitle').textContent = '新对话';
  $('#intentInput').focus();
}

function showView(view) {
  $$('[data-view]').forEach((item) => item.classList.toggle('selected', item.dataset.view === view));
  $$('[data-view-panel]').forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
  $('#workspaceTitle').textContent = VIEW_TITLES[view] || 'Agent Studio';
  $('#content').scrollTop = 0;
}

async function loadRuns(selectLatest = false) {
  const data = await api('/api/agent-runs?limit=20');
  state.runs = data.items || [];
  if (selectLatest && state.runs.length) selectRun(state.runs[0].id);
  else renderRuns();
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

  const requestedCount = clampCount($('#resultCount').value);
  state.settings.requested_count = requestedCount;
  state.busy = true;
  state.activeRunId = '';
  state.activeIntent = intent;
  state.variants = [];
  state.runError = '';
  setRunMessage('正在请求模型，请保持页面打开。');
  renderResults();
  renderRouteControls();
  $('#conversationTitle').textContent = intent;

  try {
    await persistRuntime({ requested_count: requestedCount });
    const result = await api('/api/generate', {
      method: 'POST',
      body: JSON.stringify({
        intent,
        current_document: {},
        requested_count: requestedCount,
        include_chinese: Boolean(state.settings.include_chinese),
        provider_id: provider.id,
        model: state.settings.model,
        reasoning_effort: state.settings.reasoning_effort || 'none',
      }),
    });
    state.activeRunId = result.id || '';
    state.variants = result.status === 'completed' ? result.variants || [] : [];
    state.runError = result.error?.message || '';
    if (result.status === 'completed') {
      setRunMessage(`生成完成 · ${result.model} · ${state.variants.length} 个结果`);
      showToast('生成完成');
    } else {
      setRunMessage(state.runError || '生成失败', 'error');
    }
    await loadRuns(false);
  } catch (error) {
    state.runError = error.message;
    setRunMessage(error.message, 'error');
    showToast(error.message);
  } finally {
    state.busy = false;
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
  if (!skill) return;
  button.disabled = true;
  try {
    const updated = await api(`/api/skills/${skillId}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled: !skill.enabled }),
    });
    state.skills = state.skills.map((item) => item.id === skillId ? updated : item);
    renderSkills();
    showToast(updated.enabled ? 'Skill 已启用' : 'Skill 已停用');
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
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
      showToast(`服务正常 · schema v${result.schema_version} · ${result.enabled_providers} 个供应商`);
    } catch (error) {
      setServiceStatus(false);
      showToast(error.message);
    }
  });
  $('#newChat').addEventListener('click', newChat);
  $('#generateButton').addEventListener('click', generate);
  $('#intentInput').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') generate();
  });
  $('#resultCount').addEventListener('change', async (event) => {
    const count = clampCount(event.target.value);
    state.settings.requested_count = count;
    try { await persistRuntime({ requested_count: count }); } catch (error) { showToast(error.message); }
  });
  $('#copyAll').addEventListener('click', () => copyText(state.variants.map((variant) => serialize(variant.positive_tokens)).join('\n\n'), '已复制全部英文 Prompt'));

  $('#conversationList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-run-id]');
    if (button) selectRun(button.dataset.runId);
  });
  $('#resultGrid').addEventListener('click', (event) => {
    const promptButton = event.target.closest('.copy-result');
    if (promptButton) copyText(serialize(state.variants[Number(promptButton.dataset.resultIndex)]?.positive_tokens));
    const translationButton = event.target.closest('.copy-translation');
    if (translationButton) copyText(chineseText(state.variants[Number(translationButton.dataset.resultIndex)]), '已复制中文对照');
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

  $('#providerRows').addEventListener('click', (event) => {
    const button = event.target.closest('.provider-toggle');
    if (button) toggleProvider(button.dataset.providerId, button);
  });
  $('#reasoningRows').addEventListener('click', (event) => {
    const button = event.target.closest('.reasoning-choice');
    if (!button) return;
    state.settings.reasoning_effort = button.dataset.reasoning;
    changeRoute({ reasoning_effort: state.settings.reasoning_effort });
  });
  $('#includeChineseToggle').addEventListener('click', () => setChinese(!state.settings.include_chinese));
  $('#outputLanguage').addEventListener('change', (event) => setChinese(event.target.value === 'bilingual'));
  $('#skillsRows').addEventListener('click', (event) => {
    const button = event.target.closest('.skill-toggle');
    if (button) toggleSkill(button.dataset.skillId, button);
  });
  $('#skillMode').addEventListener('change', async (event) => {
    const mode = event.target.value === 'full' ? 'full' : 'compact';
    state.settings.skill_mode = mode;
    try { await persistRuntime({ skill_mode: mode }); showToast(mode === 'full' ? '已切换完整技能' : '已切换精简技能'); } catch (error) { showToast(error.message); }
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
