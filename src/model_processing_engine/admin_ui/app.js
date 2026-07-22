"use strict";

const API_BASE = "/v1/admin";
const MANUAL_MODEL = "__manual__";

const state = {
  csrfToken: "",
  config: null,
  selectedProviderId: "",
  availableModels: [],
  verificationToken: "",
};

const elements = {
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
  timeoutSeconds: document.querySelector("#timeout-seconds"),
  transportRetries: document.querySelector("#transport-retries"),
  remoteFields: document.querySelector("#remote-fields"),
  resultBox: document.querySelector("#result-box"),
  testProvider: document.querySelector("#test-provider"),
  applyProvider: document.querySelector("#apply-provider"),
  restartOverlay: document.querySelector("#restart-overlay"),
  restartMessage: document.querySelector("#restart-message"),
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
  elements.transportRetries.value = String(provider.transportRetries);
  elements.credentialState.textContent = provider.credentialConfigured
    ? "Key 已配置；留空将使用现有值"
    : "尚未配置 Key";
  elements.activeBadge.hidden = !provider.active;
  elements.editorTitle.textContent = `配置 ${provider.id}`;
  elements.applyProvider.disabled = true;
  renderModelOptions(provider.availableModels, provider.defaultModel);
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
  elements.transportRetries.value = "2";
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
    transportRetries: Number(elements.transportRetries.value),
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
    elements.transportRetries,
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
  setButtonLoading(elements.discoverModels, true, "正在获取…");
  elements.modelState.textContent = "正在读取当前账号可用的模型列表…";
  try {
    const payload = await requestJson(`${API_BASE}/providers/models`, {
      method: "POST",
      body: JSON.stringify(collectConnectionDraft()),
    });
    const current = selectedModel();
    const nextModel = payload.models.includes(current) ? current : payload.models[0];
    renderModelOptions(payload.models, nextModel);
    invalidateVerification();
    setResult(`已获取 ${payload.models.length} 个模型，请确认默认执行模型。`, "success");
  } catch (error) {
    elements.modelState.textContent = "自动获取失败，仍可手动填写模型 ID。";
    setResult(`${error.message}；你仍可以手动填写模型 ID。`, "error");
  } finally {
    setButtonLoading(elements.discoverModels, false, "");
    updateProviderType();
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
    setResult(`连接测试通过，耗时 ${payload.elapsedMs} ms。现在可以保存并激活。`, "success");
  } catch (error) {
    state.verificationToken = "";
    setResult(error.message, "error");
  } finally {
    setButtonLoading(elements.testProvider, false, "");
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
  updateManualModelVisibility();
  invalidateVerification();
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

loadConfig().catch((error) => {
  setServiceState(false, "管理服务不可用");
  setResult(error.message, "error");
});
