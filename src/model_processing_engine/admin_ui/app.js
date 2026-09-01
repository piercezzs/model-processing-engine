"use strict";

const API_BASE = "/v1/admin";
const MANUAL_MODEL = "__manual__";

const state = {
  csrfToken: "",
  config: null,
  selectedProviderId: "",
  availableModels: [],
  modelReasoningCapabilities: {},
  modelDiscoveryRequestId: 0,
  verificationToken: "",
  historyPeriod: "day",
  historyOffset: 0,
  historyPageSize: 20,
  historyRequestId: 0,
  historyTimezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
};

const elements = {
  navItems: [...document.querySelectorAll("[data-view]")],
  providerView: document.querySelector("#provider-view"),
  executionView: document.querySelector("#execution-view"),
  serviceDot: document.querySelector("#service-dot"),
  serviceLabel: document.querySelector("#service-label"),
  providerList: document.querySelector("#provider-list"),
  addProvider: document.querySelector("#add-provider"),
  envPath: document.querySelector("#env-path"),
  editorTitle: document.querySelector("#editor-title"),
  activeBadge: document.querySelector("#active-badge"),
  form: document.querySelector("#provider-form"),
  providerPreset: document.querySelector("#provider-preset"),
  providerType: document.querySelector("#provider-type"),
  providerId: document.querySelector("#provider-id"),
  baseUrl: document.querySelector("#base-url"),
  chatPath: document.querySelector("#chat-path"),
  modelsPath: document.querySelector("#models-path"),
  apiKey: document.querySelector("#api-key"),
  toggleKey: document.querySelector("#toggle-key"),
  credentialState: document.querySelector("#credential-state"),
  modelSelect: document.querySelector("#model-select"),
  modelManual: document.querySelector("#model-manual"),
  modelState: document.querySelector("#model-state"),
  discoverModels: document.querySelector("#discover-models"),
  reasoningEffort: document.querySelector("#reasoning-effort"),
  reasoningState: document.querySelector("#reasoning-state"),
  timeoutSeconds: document.querySelector("#timeout-seconds"),
  maxConcurrency: document.querySelector("#max-concurrency"),
  transportRetries: document.querySelector("#transport-retries"),
  nativeJsonSchema: document.querySelector("#native-json-schema"),
  remoteFields: document.querySelector("#remote-fields"),
  resultBox: document.querySelector("#result-box"),
  testProvider: document.querySelector("#test-provider"),
  applyProvider: document.querySelector("#apply-provider"),
  restartOverlay: document.querySelector("#restart-overlay"),
  restartMessage: document.querySelector("#restart-message"),
  refreshHistory: document.querySelector("#refresh-history"),
  historyTotal: document.querySelector("#history-total"),
  historyTokens: document.querySelector("#history-tokens"),
  historyUsageCoverage: document.querySelector("#history-usage-coverage"),
  historyCacheHits: document.querySelector("#history-cache-hits"),
  historyProviderCacheTokens: document.querySelector("#history-provider-cache-tokens"),
  historyProviderCacheCoverage: document.querySelector("#history-provider-cache-coverage"),
  historyProviderCalls: document.querySelector("#history-provider-calls"),
  historyTransportRetries: document.querySelector("#history-transport-retries"),
  historyStatusSummary: document.querySelector("#history-status-summary"),
  historyCacheCoverage: document.querySelector("#history-cache-coverage"),
  historyPeriodButtons: [...document.querySelectorAll("[data-period]")],
  historyAnchor: document.querySelector("#history-anchor"),
  historyAnchorLabel: document.querySelector("#history-anchor-label"),
  historyKind: document.querySelector("#history-kind"),
  historyProvider: document.querySelector("#history-provider"),
  historyModel: document.querySelector("#history-model"),
  historyTimezone: document.querySelector("#history-timezone"),
  historyTrend: document.querySelector("#history-trend"),
  historyTrendTitle: document.querySelector("#history-trend-title"),
  historyTrendDescription: document.querySelector("#history-trend-description"),
  historyModelTable: document.querySelector("#history-model-table"),
  historyState: document.querySelector("#history-state"),
  historyList: document.querySelector("#history-list"),
  historyPrevious: document.querySelector("#history-previous"),
  historyNext: document.querySelector("#history-next"),
  historyPageState: document.querySelector("#history-page-state"),
};

const numberFormatter = new Intl.NumberFormat("zh-CN");
const compactNumberFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const REASONING_LABELS = {
  auto: "自动（模型默认）",
  none: "None（不启用推理）",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra High",
  max: "Maximum",
};

function setServiceState(online, label) {
  elements.serviceDot.classList.toggle("online", online);
  elements.serviceLabel.textContent = label;
}

function setResult(message, kind = "neutral") {
  elements.resultBox.textContent = message;
  elements.resultBox.className = `result-box ${kind}`;
}

function setButtonLoading(button, loading, loadingLabel) {
  if (loading) {
    button.dataset.originalLabel = button.textContent;
    button.textContent = loadingLabel;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalLabel || button.textContent;
    button.disabled = false;
  }
}

function formatNumber(value) {
  return numberFormatter.format(Math.max(0, Number(value) || 0));
}

function formatRatio(numerator, denominator) {
  const total = Math.max(0, Number(denominator) || 0);
  if (!total) {
    return "0.0%";
  }
  const value = Math.max(0, Number(numerator) || 0);
  return `${((value / total) * 100).toFixed(1)}%`;
}

function formatHistoryTime(value) {
  if (!value) {
    return "时间未知";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", {hour12: false});
}

function formatDuration(value) {
  const milliseconds = Math.max(0, Number(value) || 0);
  if (!milliseconds) {
    return "耗时未知";
  }
  if (milliseconds < 1000) {
    return `${Math.round(milliseconds)} ms`;
  }
  if (milliseconds < 60000) {
    return `${(milliseconds / 1000).toFixed(1)} 秒`;
  }
  return `${(milliseconds / 60000).toFixed(1)} 分钟`;
}

function localAnchor(period, date = new Date()) {
  const year = String(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  if (period === "year") {
    return year;
  }
  if (period === "month") {
    return `${year}-${month}`;
  }
  return `${year}-${month}-${day}`;
}

function updateHistoryAnchorControl() {
  const current = elements.historyAnchor.value;
  const currentYear = /^\d{4}/.test(current) ? current.slice(0, 4) : "";
  const currentMonth = /^\d{4}-\d{2}/.test(current) ? current.slice(0, 7) : "";
  const currentDay = /^\d{4}-\d{2}-\d{2}$/.test(current) ? current : "";
  if (state.historyPeriod === "year") {
    elements.historyAnchor.type = "number";
    elements.historyAnchor.min = "2000";
    elements.historyAnchor.max = "9998";
    elements.historyAnchor.step = "1";
    elements.historyAnchor.value = currentYear || localAnchor("year");
    elements.historyAnchorLabel.textContent = "年份";
  } else if (state.historyPeriod === "month") {
    elements.historyAnchor.type = "month";
    elements.historyAnchor.removeAttribute("min");
    elements.historyAnchor.removeAttribute("max");
    elements.historyAnchor.removeAttribute("step");
    elements.historyAnchor.value =
      currentMonth ||
      (currentYear ? `${currentYear}-${localAnchor("month").slice(5)}` : localAnchor("month"));
    elements.historyAnchorLabel.textContent = "月份";
  } else {
    elements.historyAnchor.type = "date";
    elements.historyAnchor.removeAttribute("min");
    elements.historyAnchor.removeAttribute("max");
    elements.historyAnchor.removeAttribute("step");
    elements.historyAnchor.value =
      currentDay ||
      (currentYear ? `${currentYear}-${localAnchor("day").slice(5)}` : localAnchor("day"));
    elements.historyAnchorLabel.textContent = "日期";
  }
  for (const button of elements.historyPeriodButtons) {
    button.classList.toggle("active", button.dataset.period === state.historyPeriod);
  }
}

function currentView() {
  return window.location.hash === "#executions" ? "executions" : "providers";
}

function setView(view, {updateHash = true} = {}) {
  const nextView = view === "executions" ? "executions" : "providers";
  elements.providerView.hidden = nextView !== "providers";
  elements.executionView.hidden = nextView !== "executions";
  for (const item of elements.navItems) {
    const active = item.dataset.view === nextView;
    item.classList.toggle("active", active);
    item.setAttribute("aria-current", active ? "page" : "false");
  }
  if (updateHash) {
    const nextHash = nextView === "executions" ? "#executions" : "#providers";
    if (window.location.hash !== nextHash) {
      window.history.replaceState(null, "", nextHash);
    }
  }
  if (nextView === "executions") {
    loadHistory();
  }
}

function historyStatusLabel(status) {
  return {
    queued: "等待中",
    running: "执行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
  }[status] || status || "未知";
}

function historyTaskLabel(item) {
  if (item.kind === "provider_test") {
    return "连接测试";
  }
  const task = item.task || {};
  return [task.namespace, task.id].filter(Boolean).join(" / ") || "未命名任务";
}

function replaceOptions(select, options, selectedValue) {
  select.replaceChildren();
  const nextOptions = [...options];
  if (
    selectedValue &&
    !nextOptions.some((option) => option.value === selectedValue)
  ) {
    nextOptions.push({
      value: selectedValue,
      label: `${selectedValue}（当前筛选）`,
    });
  }
  for (const optionData of nextOptions) {
    const option = document.createElement("option");
    option.value = optionData.value;
    option.textContent = optionData.label;
    select.append(option);
  }
  select.value = selectedValue || "";
}

function renderHistoryFacets(facets) {
  const providers = Array.isArray(facets?.providers) ? facets.providers : [];
  const selectedProvider = elements.historyProvider.value;
  const selectedModel = elements.historyModel.value;
  replaceOptions(
    elements.historyProvider,
    [
      {value: "", label: "全部 Provider"},
      ...providers.map((provider) => ({
        value: provider.id,
        label: provider.id,
      })),
    ],
    selectedProvider,
  );
  const activeProvider = elements.historyProvider.value;
  const models = activeProvider
    ? providers.find((provider) => provider.id === activeProvider)?.models || []
    : [...new Set(providers.flatMap((provider) => provider.models || []))]
      .sort((left, right) => left.localeCompare(right));
  replaceOptions(
    elements.historyModel,
    [
      {value: "", label: "全部模型"},
      ...models.map((model) => ({value: model, label: model})),
    ],
    selectedModel,
  );
}

function renderTrend(series, period) {
  elements.historyTrend.replaceChildren();
  const items = Array.isArray(series) ? series : [];
  const titles = {
    day: ["当天调用趋势", "按小时聚合执行任务与 Provider 调用"],
    month: ["本月调用趋势", "按日期聚合执行任务与 Provider 调用"],
    year: ["全年调用趋势", "按月份聚合执行任务与 Provider 调用"],
  };
  const [title, description] = titles[period] || titles.day;
  elements.historyTrendTitle.textContent = title;
  elements.historyTrendDescription.textContent = description;
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "history-chart-empty";
    empty.textContent = "当前周期暂无趋势数据";
    elements.historyTrend.append(empty);
    return;
  }
  const maximum = Math.max(
    1,
    ...items.flatMap((item) => [
      Number(item.executions) || 0,
      Number(item.providerCalls) || 0,
    ]),
  );
  const labelEvery = period === "day" ? 3 : period === "month" ? 5 : 1;
  items.forEach((item, index) => {
    const bucket = document.createElement("div");
    bucket.className = "trend-bucket";
    bucket.title =
      `${item.label}：${formatNumber(item.executions)} 次执行，` +
      `${formatNumber(item.providerCalls)} 次 Provider 调用，` +
      `${formatNumber(item.totalTokens)} Token`;
    const bars = document.createElement("div");
    bars.className = "trend-bars";
    for (const [kind, value] of [
      ["execution", item.executions],
      ["provider", item.providerCalls],
    ]) {
      const bar = document.createElement("span");
      bar.className = `trend-bar ${kind}`;
      const numeric = Math.max(0, Number(value) || 0);
      bar.style.height = numeric
        ? `${Math.max(3, numeric / maximum * 100)}%`
        : "2px";
      bars.append(bar);
    }
    const label = document.createElement("span");
    label.className = "trend-label";
    const showLabel =
      index === 0 ||
      index === items.length - 1 ||
      index % labelEvery === 0;
    label.textContent = showLabel ? item.label : "";
    bucket.append(bars, label);
    elements.historyTrend.append(bucket);
  });
}

function renderModelBreakdown(models) {
  elements.historyModelTable.replaceChildren();
  const items = Array.isArray(models) ? models : [];
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.className = "model-table-empty";
    cell.colSpan = 5;
    cell.textContent = "当前筛选条件下暂无模型调用";
    row.append(cell);
    elements.historyModelTable.append(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const identityCell = document.createElement("td");
    const identity = document.createElement("span");
    identity.className = "model-identity";
    const provider = document.createElement("strong");
    provider.textContent = item.providerId || "Provider 未知";
    const model = document.createElement("span");
    model.textContent = item.model || "模型未知";
    identity.append(provider, model);
    identityCell.append(identity);

    const executions = document.createElement("td");
    executions.textContent = formatNumber(item.executions);
    const calls = document.createElement("td");
    calls.textContent = formatNumber(item.providerCalls);
    const successRate = document.createElement("td");
    successRate.className = "model-success-rate";
    successRate.textContent = `${Number(item.successRate || 0).toFixed(1)}%`;
    const tokens = document.createElement("td");
    tokens.textContent = compactNumberFormatter.format(
      Math.max(0, Number(item.usage?.totalTokens) || 0),
    );
    row.append(identityCell, executions, calls, successRate, tokens);
    elements.historyModelTable.append(row);
  }
}

function renderHistoryStatistics(payload) {
  const summary = payload.summary || {};
  const usage = summary.usage || {};
  elements.historyTotal.textContent = formatNumber(summary.total);
  elements.historyTokens.textContent = formatNumber(usage.totalTokens);
  elements.historyUsageCoverage.textContent =
    `输入 ${formatNumber(usage.inputTokens)} · 输出 ${formatNumber(usage.outputTokens)}`;
  elements.historyCacheHits.textContent = formatNumber(summary.cacheHits);
  elements.historyCacheCoverage.textContent =
    `${formatNumber(summary.cacheHits)} 次跳过 Provider`;
  elements.historyProviderCacheTokens.textContent = formatNumber(usage.cacheReadInputTokens);
  elements.historyProviderCacheCoverage.textContent =
    `${formatNumber(summary.providerCacheHitExecutions)} 条命中 · ` +
    `${formatRatio(usage.cacheReadInputTokens, usage.inputTokens)} 输入`;
  elements.historyProviderCalls.textContent = formatNumber(summary.providerCallCount);
  elements.historyTransportRetries.textContent =
    `${formatNumber(summary.contractRepairs)} 次合同修复 · ` +
    `${formatNumber(summary.transportRetries)} 次重试`;
  elements.historyStatusSummary.textContent =
    `${formatNumber(summary.succeeded)} 成功 · ${formatNumber(summary.failed)} 失败 · ` +
    `${formatNumber(summary.cancelled)} 取消` +
    (summary.averageElapsedMs
      ? ` · 平均 ${formatDuration(summary.averageElapsedMs)}`
      : "");
  renderHistoryFacets(payload.facets || {});
  renderTrend(payload.series, payload.period?.kind || state.historyPeriod);
  renderModelBreakdown(payload.models);
}

function renderHistory(payload) {
  elements.historyList.replaceChildren();

  const items = Array.isArray(payload.items) ? payload.items : [];
  const pagination = payload.pagination || {};
  const total = Math.max(0, Number(pagination.total) || 0);
  const page = Math.floor(state.historyOffset / state.historyPageSize) + 1;
  const totalPages = Math.max(1, Math.ceil(total / state.historyPageSize));
  elements.historyPageState.textContent = `第 ${page} / ${totalPages} 页`;
  elements.historyPrevious.disabled = state.historyOffset <= 0;
  elements.historyNext.disabled = !pagination.hasMore;
  if (!items.length) {
    elements.historyState.textContent = "当前筛选条件下没有调用记录";
    const empty = document.createElement("p");
    empty.className = "history-list-empty";
    empty.textContent = "可以切换日期、调用类型、Provider 或模型后重试。";
    elements.historyList.append(empty);
    return;
  }
  const kindLabel = elements.historyKind.selectedOptions[0]?.textContent || "调用";
  elements.historyState.textContent =
    `共 ${formatNumber(total)} 条${kindLabel} · 当前显示 ${formatNumber(items.length)} 条`;
  for (const item of items) {
    const record = document.createElement("article");
    record.className = "history-record";

    const time = document.createElement("time");
    const completedAt = item.timing?.completedAt || item.timing?.createdAt || "";
    time.dateTime = completedAt;
    time.textContent = formatHistoryTime(completedAt);

    const identity = document.createElement("div");
    identity.className = "history-identity";
    const taskName = document.createElement("strong");
    taskName.textContent = historyTaskLabel(item);
    const provider = document.createElement("span");
    provider.textContent = [item.provider?.id, item.provider?.model]
      .filter(Boolean)
      .join(" · ") || "Provider 未知";
    identity.append(taskName, provider);

    const status = document.createElement("span");
    status.className = `history-status ${item.status || "unknown"}`;
    status.textContent = historyStatusLabel(item.status);

    const usageCell = document.createElement("div");
    usageCell.className = "history-usage";
    const tokenCount = document.createElement("strong");
    tokenCount.textContent = item.usage?.available
      ? `${formatNumber(item.usage.totalTokens)} Token`
      : "Token 未提供";
    const elapsed = document.createElement("span");
    elapsed.textContent = formatDuration(item.timing?.elapsedMs);
    usageCell.append(tokenCount, elapsed);

    const auditCell = document.createElement("div");
    auditCell.className = "history-audit";
    const auditState = document.createElement("strong");
    const providerCacheTokens = Math.max(
      0,
      Number(item.usage?.cacheReadInputTokens) || 0,
    );
    if (item.cache?.hit) {
      auditState.textContent = "MPE 结果缓存命中";
    } else if (item.timing?.contractRepairs) {
      auditState.textContent =
        `合同修复 ${formatNumber(item.timing.contractRepairs)} 次`;
    } else if (providerCacheTokens) {
      auditState.textContent =
        `Prompt Cache ${formatNumber(providerCacheTokens)}`;
    } else {
      auditState.textContent = "未命中缓存";
    }
    const providerCalls = document.createElement("span");
    providerCalls.textContent =
      `${formatNumber(item.timing?.providerCallCount)} 次 Provider 调用`;
    auditCell.append(auditState, providerCalls);

    record.append(time, identity, status, usageCell, auditCell);
    if (item.error) {
      const error = document.createElement("p");
      error.className = "history-error";
      error.textContent = item.error;
      record.append(error);
    }
    elements.historyList.append(record);
  }
}

function historyStatsPath() {
  const parameters = new URLSearchParams({
    period: state.historyPeriod,
    anchor: elements.historyAnchor.value,
    timezone: state.historyTimezone,
    kind: elements.historyKind.value,
  });
  if (elements.historyProvider.value) {
    parameters.set("providerId", elements.historyProvider.value);
  }
  if (elements.historyModel.value) {
    parameters.set("model", elements.historyModel.value);
  }
  return `${API_BASE}/execution-stats?${parameters.toString()}`;
}

function historyListPath(period) {
  const parameters = new URLSearchParams({
    limit: String(state.historyPageSize),
    offset: String(state.historyOffset),
    createdFrom: String(new Date(period.start).getTime() / 1000),
    createdTo: String(new Date(period.end).getTime() / 1000),
    includeSummary: "false",
  });
  if (elements.historyKind.value !== "all") {
    parameters.set("kind", elements.historyKind.value);
  }
  if (elements.historyProvider.value) {
    parameters.set("providerId", elements.historyProvider.value);
  }
  if (elements.historyModel.value) {
    parameters.set("model", elements.historyModel.value);
  }
  return `${API_BASE}/executions?${parameters.toString()}`;
}

async function loadHistory({showLoading = false, resetPage = false} = {}) {
  if (!elements.historyAnchor.value) {
    return;
  }
  if (resetPage) {
    state.historyOffset = 0;
  }
  const requestId = state.historyRequestId + 1;
  state.historyRequestId = requestId;
  if (showLoading) {
    setButtonLoading(elements.refreshHistory, true, "正在刷新…");
  }
  elements.historyState.textContent = "正在读取调用记录…";
  try {
    const statistics = await requestJson(historyStatsPath());
    if (requestId !== state.historyRequestId) {
      return;
    }
    renderHistoryStatistics(statistics);
    const payload = await requestJson(historyListPath(statistics.period));
    if (requestId !== state.historyRequestId) {
      return;
    }
    renderHistory(payload);
  } catch (error) {
    elements.historyState.textContent = `调用记录读取失败：${error.message}`;
    elements.historyList.replaceChildren();
  } finally {
    if (showLoading && requestId === state.historyRequestId) {
      setButtonLoading(elements.refreshHistory, false, "");
    }
  }
}

async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (options.method && options.method !== "GET") {
    headers.set("X-MPE-CSRF", state.csrfToken);
  }
  const response = await fetch(path, {...options, headers, cache: "no-store"});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

function invalidateVerification() {
  state.verificationToken = "";
  elements.applyProvider.disabled = true;
  if (!elements.testProvider.disabled) {
    setResult("配置已发生变化，请重新测试连接。", "neutral");
  }
}

function presetById(presetId) {
  return state.config?.providerPresets.find((item) => item.id === presetId);
}

function renderPresetOptions() {
  elements.providerPreset.replaceChildren();
  for (const preset of state.config.providerPresets) {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = preset.label;
    elements.providerPreset.append(option);
  }
}

function providerLabel(provider) {
  const preset = presetById(provider.presetId);
  const model = provider.defaultModel || (provider.type === "mock" ? "离线测试" : "未设置模型");
  return preset ? `${preset.label} · ${model}` : model;
}

function renderProviderList() {
  elements.providerList.replaceChildren();
  for (const provider of state.config.providers) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "provider-item";
    button.classList.toggle("selected", provider.id === state.selectedProviderId);
    button.dataset.providerId = provider.id;

    const name = document.createElement("strong");
    name.textContent = provider.id;
    const model = document.createElement("span");
    model.textContent = providerLabel(provider);
    button.append(name, model);
    if (provider.active) {
      const active = document.createElement("span");
      active.className = "active-line";
      active.textContent = "● 当前激活";
      button.append(active);
    }
    button.addEventListener("click", () => selectProvider(provider.id));
    elements.providerList.append(button);
  }
}

function renderModelOptions(models, selectedModel = "") {
  const uniqueModels = [...new Set(models.filter((model) => typeof model === "string" && model.trim()))];
  state.availableModels = uniqueModels;
  elements.modelSelect.replaceChildren();
  for (const modelId of uniqueModels) {
    const option = document.createElement("option");
    option.value = modelId;
    option.textContent = modelId;
    elements.modelSelect.append(option);
  }
  const manual = document.createElement("option");
  manual.value = MANUAL_MODEL;
  manual.textContent = "手动填写模型 ID…";
  elements.modelSelect.append(manual);

  if (selectedModel && uniqueModels.includes(selectedModel)) {
    elements.modelSelect.value = selectedModel;
    elements.modelManual.value = "";
  } else {
    elements.modelSelect.value = MANUAL_MODEL;
    elements.modelManual.value = selectedModel;
  }
  updateManualModelVisibility();
  elements.modelState.textContent = uniqueModels.length
    ? `已保存或获取 ${uniqueModels.length} 个模型；请选择默认执行模型。`
    : "尚未获取模型；也可以手动填写模型 ID。";
}

function updateManualModelVisibility() {
  const manual = elements.modelSelect.value === MANUAL_MODEL;
  elements.modelManual.hidden = !manual;
  elements.modelManual.required = manual;
}

function selectedModel() {
  return elements.modelSelect.value === MANUAL_MODEL
    ? elements.modelManual.value.trim()
    : elements.modelSelect.value.trim();
}

function renderReasoningOptions(preferred = "auto", {announceChange = false} = {}) {
  const model = selectedModel();
  const capability = state.modelReasoningCapabilities[model] || {
    configurable: false,
    options: ["auto"],
    modelDefault: null,
    wireParameter: null,
  };
  const options = Array.isArray(capability.options) && capability.options.length
    ? capability.options
    : ["auto"];
  const selected = options.includes(preferred) ? preferred : "auto";
  elements.reasoningEffort.replaceChildren();
  for (const value of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = REASONING_LABELS[value] || value;
    elements.reasoningEffort.append(option);
  }
  elements.reasoningEffort.value = selected;
  elements.reasoningEffort.disabled = options.length === 1;
  if (announceChange && selected !== preferred) {
    elements.reasoningState.textContent = "所选模型不支持原推理强度，已恢复为自动。";
    return;
  }
  if (!capability.configurable) {
    elements.reasoningState.textContent = model
      ? "该模型在当前协议下没有已确认的推理强度参数，将使用模型默认行为。"
      : "选择模型后，将显示该模型支持的推理强度。";
    return;
  }
  const defaultLabel = capability.modelDefault
    ? (REASONING_LABELS[capability.modelDefault] || capability.modelDefault)
    : "由模型决定";
  elements.reasoningState.textContent = `自动不会发送强度参数；该模型的已知默认值为 ${defaultLabel}。`;
}

function selectProvider(providerId) {
  const provider = state.config.providers.find((item) => item.id === providerId);
  if (!provider) {
    return;
  }
  state.selectedProviderId = provider.id;
  state.verificationToken = "";
  elements.providerPreset.value = provider.presetId;
  elements.providerType.value = provider.type;
  elements.providerId.value = provider.id;
  elements.baseUrl.value = provider.baseUrl;
  elements.chatPath.value = provider.chatCompletionsPath;
  elements.modelsPath.value = provider.modelsPath;
  elements.apiKey.value = "";
  elements.timeoutSeconds.value = String(provider.timeoutSeconds);
  elements.maxConcurrency.value = String(provider.maxConcurrency);
  elements.transportRetries.value = String(provider.transportRetries);
  elements.nativeJsonSchema.checked = Boolean(provider.nativeJsonSchema);
  state.modelReasoningCapabilities = provider.modelReasoningCapabilities || {};
  elements.credentialState.textContent = provider.credentialConfigured
    ? "Key 已配置；留空将使用现有值"
    : "尚未配置 Key";
  elements.activeBadge.hidden = !provider.active;
  elements.editorTitle.textContent = `配置 ${provider.id}`;
  elements.applyProvider.disabled = true;
  renderModelOptions(provider.availableModels, provider.defaultModel);
  renderReasoningOptions(provider.defaultReasoningEffort || "auto");
  updateProviderType();
  setResult("修改配置后，请先测试连接。", "neutral");
  renderProviderList();
}

function applyPreset(presetId, {updateProviderId = false} = {}) {
  const preset = presetById(presetId);
  if (!preset) {
    return;
  }
  elements.providerPreset.value = preset.id;
  elements.providerType.value = preset.type;
  elements.baseUrl.value = preset.baseUrl;
  elements.chatPath.value = preset.chatCompletionsPath;
  elements.modelsPath.value = preset.modelsPath;
  if (updateProviderId) {
    elements.providerId.value = preset.providerId;
  }
  renderModelOptions(preset.type === "mock" ? ["schema-sample-v1"] : [], preset.type === "mock" ? "schema-sample-v1" : "");
  state.modelReasoningCapabilities = {};
  renderReasoningOptions("auto");
  updateProviderType();
}

function newProvider() {
  state.selectedProviderId = "";
  state.verificationToken = "";
  elements.form.reset();
  elements.credentialState.textContent = "尚未配置 Key";
  elements.activeBadge.hidden = true;
  elements.editorTitle.textContent = "新增模型平台";
  elements.applyProvider.disabled = true;
  applyPreset("custom", {updateProviderId: true});
  elements.timeoutSeconds.value = "60";
  elements.maxConcurrency.value = "8";
  elements.transportRetries.value = "2";
  elements.nativeJsonSchema.checked = false;
  setResult("选择服务商并填写 Key 后，可以先获取模型，再测试连接。", "neutral");
  renderProviderList();
  elements.providerPreset.focus();
}

function updateProviderType() {
  const isMock = elements.providerType.value === "mock";
  elements.remoteFields.hidden = isMock;
  elements.baseUrl.required = !isMock;
  elements.discoverModels.disabled = false;
  elements.discoverModels.textContent = isMock ? "载入离线模型" : "检测并获取模型";
}

function collectConnectionDraft() {
  const payload = {
    providerId: elements.providerId.value.trim(),
    presetId: elements.providerPreset.value,
    type: elements.providerType.value,
    baseUrl: elements.baseUrl.value.trim(),
    chatCompletionsPath: elements.chatPath.value.trim() || "/chat/completions",
    modelsPath: elements.modelsPath.value.trim() || "/models",
    timeoutSeconds: Number(elements.timeoutSeconds.value),
    maxConcurrency: Number(elements.maxConcurrency.value),
    transportRetries: Number(elements.transportRetries.value),
    nativeJsonSchema: elements.nativeJsonSchema.checked,
  };
  if (elements.apiKey.value) {
    payload.apiKey = elements.apiKey.value;
  }
  return payload;
}

function collectDraft() {
  const model = selectedModel();
  const availableModels = [...state.availableModels];
  if (model && !availableModels.includes(model)) {
    availableModels.push(model);
  }
  return {
    ...collectConnectionDraft(),
    model,
    availableModels,
    defaultReasoningEffort: elements.reasoningEffort.value || "auto",
  };
}

function reportConnectionValidity() {
  const controls = [
    elements.providerId,
    elements.providerType,
    elements.baseUrl,
    elements.chatPath,
    elements.modelsPath,
    elements.timeoutSeconds,
    elements.maxConcurrency,
    elements.transportRetries,
    elements.reasoningEffort,
  ];
  for (const control of controls) {
    if (!control.checkValidity()) {
      control.reportValidity();
      return false;
    }
  }
  return true;
}

async function discoverModels() {
  if (!reportConnectionValidity()) {
    return;
  }
  const requestId = ++state.modelDiscoveryRequestId;
  setButtonLoading(elements.discoverModels, true, "正在获取…");
  elements.modelState.textContent = "正在读取当前账号可用的模型列表…";
  try {
    const payload = await requestJson(`${API_BASE}/providers/models`, {
      method: "POST",
      body: JSON.stringify(collectConnectionDraft()),
    });
    if (requestId !== state.modelDiscoveryRequestId) {
      return;
    }
    const current = selectedModel();
    const nextModel = payload.models.includes(current) ? current : payload.models[0];
    const previousEffort = elements.reasoningEffort.value || "auto";
    state.modelReasoningCapabilities = payload.modelReasoningCapabilities || {};
    renderModelOptions(payload.models, nextModel);
    renderReasoningOptions(previousEffort, {announceChange: true});
    invalidateVerification();
    setResult(`已获取 ${payload.models.length} 个模型，请确认默认执行模型。`, "success");
  } catch (error) {
    if (requestId !== state.modelDiscoveryRequestId) {
      return;
    }
    elements.modelState.textContent = "自动获取失败，仍可手动填写模型 ID。";
    setResult(`${error.message}；你仍可以手动填写模型 ID。`, "error");
  } finally {
    if (requestId === state.modelDiscoveryRequestId) {
      setButtonLoading(elements.discoverModels, false, "");
      updateProviderType();
    }
  }
}

async function testProvider() {
  if (!elements.form.reportValidity()) {
    return;
  }
  if (!selectedModel()) {
    setResult("请选择或填写默认执行模型。", "error");
    return;
  }
  setButtonLoading(elements.testProvider, true, "正在测试…");
  elements.applyProvider.disabled = true;
  setResult("正在验证服务商、模型和 Key，请稍候。", "neutral");
  try {
    const payload = await requestJson(`${API_BASE}/providers/test`, {
      method: "POST",
      body: JSON.stringify(collectDraft()),
    });
    state.verificationToken = payload.verificationToken;
    elements.applyProvider.disabled = false;
    const tokenNote = payload.usage?.available
      ? `，本次 ${formatNumber(payload.usage.totalTokens)} Token`
      : "，Provider 未返回 Token usage";
    setResult(`连接测试通过，耗时 ${payload.elapsedMs} ms${tokenNote}。现在可以保存并激活。`, "success");
  } catch (error) {
    state.verificationToken = "";
    setResult(error.message, "error");
  } finally {
    setButtonLoading(elements.testProvider, false, "");
    if (!elements.executionView.hidden) {
      loadHistory();
    }
  }
}

async function applyProvider() {
  if (!state.verificationToken) {
    setResult("请先完成连接测试。", "error");
    return;
  }
  setButtonLoading(elements.applyProvider, true, "正在保存…");
  elements.testProvider.disabled = true;
  try {
    const payload = await requestJson(`${API_BASE}/providers/apply`, {
      method: "POST",
      body: JSON.stringify({...collectDraft(), verificationToken: state.verificationToken}),
    });
    state.verificationToken = "";
    elements.restartOverlay.hidden = false;
    elements.restartMessage.textContent = `${payload.providerId} / ${payload.model} 已保存，正在重启 MPE。`;
    await waitForRestart();
  } catch (error) {
    setResult(error.message, "error");
    elements.testProvider.disabled = false;
    setButtonLoading(elements.applyProvider, false, "");
  }
}

async function waitForRestart() {
  const deadline = Date.now() + 30000;
  await new Promise((resolve) => window.setTimeout(resolve, 700));
  while (Date.now() < deadline) {
    try {
      const response = await fetch("/v1/health", {cache: "no-store"});
      const health = await response.json();
      if (response.ok && health.app === "model-processing-engine" && health.status === "ok") {
        await loadConfig();
        if (!elements.executionView.hidden) {
          await loadHistory();
        }
        elements.restartOverlay.hidden = true;
        setResult("配置已经生效，MPE 服务已恢复。", "success");
        return;
      }
    } catch (_error) {
      // A short connection failure is expected while the managed process restarts.
    }
    await new Promise((resolve) => window.setTimeout(resolve, 700));
  }
  elements.restartOverlay.hidden = true;
  elements.testProvider.disabled = false;
  setButtonLoading(elements.applyProvider, false, "");
  setResult("配置已保存，但服务未在 30 秒内恢复。请运行 start_mpe.command 检查状态。", "error");
}

async function loadConfig() {
  const payload = await requestJson(`${API_BASE}/config`);
  state.csrfToken = payload.csrfToken;
  state.config = payload.config;
  elements.envPath.textContent = payload.config.envPath;
  setServiceState(true, `MPE ${payload.service.version} · 运行中`);
  renderPresetOptions();
  const activeId = payload.config.activeProviderId;
  const selectedExists = payload.config.providers.some((item) => item.id === state.selectedProviderId);
  const nextId = selectedExists ? state.selectedProviderId : activeId || payload.config.providers[0]?.id;
  if (nextId) {
    selectProvider(nextId);
  } else {
    newProvider();
  }
  elements.testProvider.disabled = false;
  setButtonLoading(elements.applyProvider, false, "");
}

elements.addProvider.addEventListener("click", newProvider);
elements.providerPreset.addEventListener("change", () => {
  applyPreset(elements.providerPreset.value, {updateProviderId: !state.selectedProviderId});
  invalidateVerification();
});
elements.providerType.addEventListener("change", () => {
  if (elements.providerType.value === "mock") {
    elements.providerPreset.value = "mock";
  } else if (elements.providerPreset.value === "mock") {
    elements.providerPreset.value = "custom";
  }
  updateProviderType();
  invalidateVerification();
});
elements.modelSelect.addEventListener("change", () => {
  const previousEffort = elements.reasoningEffort.value || "auto";
  updateManualModelVisibility();
  renderReasoningOptions(previousEffort, {announceChange: true});
  invalidateVerification();
});
elements.modelManual.addEventListener("input", () => {
  const previousEffort = elements.reasoningEffort.value || "auto";
  renderReasoningOptions(previousEffort, {announceChange: true});
});
elements.form.addEventListener("input", invalidateVerification);
elements.discoverModels.addEventListener("click", discoverModels);
elements.testProvider.addEventListener("click", testProvider);
elements.applyProvider.addEventListener("click", applyProvider);
elements.toggleKey.addEventListener("click", () => {
  const showing = elements.apiKey.type === "text";
  elements.apiKey.type = showing ? "password" : "text";
  elements.toggleKey.textContent = showing ? "显示" : "隐藏";
});
for (const item of elements.navItems) {
  item.addEventListener("click", () => setView(item.dataset.view));
}
for (const button of elements.historyPeriodButtons) {
  button.addEventListener("click", () => {
    state.historyPeriod = button.dataset.period;
    updateHistoryAnchorControl();
    loadHistory({resetPage: true});
  });
}
elements.historyAnchor.addEventListener("change", () => loadHistory({resetPage: true}));
elements.historyKind.addEventListener("change", () => loadHistory({resetPage: true}));
elements.historyProvider.addEventListener("change", () => {
  elements.historyModel.value = "";
  loadHistory({resetPage: true});
});
elements.historyModel.addEventListener("change", () => loadHistory({resetPage: true}));
elements.refreshHistory.addEventListener("click", () => loadHistory({showLoading: true}));
elements.historyPrevious.addEventListener("click", () => {
  state.historyOffset = Math.max(0, state.historyOffset - state.historyPageSize);
  loadHistory();
});
elements.historyNext.addEventListener("click", () => {
  state.historyOffset += state.historyPageSize;
  loadHistory();
});
window.addEventListener("hashchange", () => setView(currentView(), {updateHash: false}));

elements.historyAnchor.value = localAnchor(state.historyPeriod);
elements.historyTimezone.textContent =
  `统计时区：${state.historyTimezone}（跟随设备）`;
updateHistoryAnchorControl();

loadConfig().catch((error) => {
  setServiceState(false, "管理服务不可用");
  setResult(error.message, "error");
});
setView(currentView());
