"use strict";

/* ============================================================
   Русификатор игр — интерфейс. Связь с Python: window.pywebview.api
   ============================================================ */

const ICONS = {
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  layers: '<path d="M12 3 2 8l10 5 10-5-10-5Z"/><path d="m2 16 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9 7 7M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  "folder-open": '<path d="M3 17V7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1"/><path d="M3 17l2.5-5.6A2 2 0 0 1 7.3 10H21l-2.7 7.8A2 2 0 0 1 16.4 19H5a2 2 0 0 1-2-2Z"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx=".6"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  chip: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/><path d="M19 15l.7 1.8 1.8.7-1.8.7L19 20l-.7-1.8-1.8-.7 1.8-.7Z"/><path d="M5 3.5l.5 1.3 1.3.5-1.3.5L5 7.1l-.5-1.3-1.3-.5 1.3-.5Z"/>',
  cloud: '<path d="M17.5 19a4.5 4.5 0 1 0-1.4-8.8A6 6 0 0 0 4.5 12 3.5 3.5 0 0 0 5 19Z"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M4 21h16"/>',
  monitor: '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7Z"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  play: '<path d="m7 4 13 8-13 8Z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  link: '<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  "x-circle": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z"/>',
  gamepad: '<rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 11v3M5.5 12.5h3M15.5 12h.01M18 13.5h.01"/>',
};

const STAGES = [
  ["detect", "Движок"], ["prepare", "Подготовка"], ["extract", "Извлечение"],
  ["translator", "Переводчик"], ["translate", "Перевод"], ["inject", "Внедрение"], ["font", "Шрифт"],
];

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const api = () => window.pywebview.api;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<span class="i">${svg(name)}</span>`;
const svg = (name) => `<svg viewBox="0 0 24 24">${ICONS[name] || ""}</svg>`;
const gb = (bytes) => (bytes / 1e9).toFixed(bytes > 1e10 ? 0 : 1) + " ГБ";
const mb = (bytes) => Math.round(bytes / 1048576) + " МБ";
const size = (bytes) => (bytes >= 1e9 ? gb(bytes) : mb(bytes));

function paintIcons(root = document) {
  $$(".i[data-icon]", root).forEach((el) => { el.innerHTML = svg(el.dataset.icon); el.removeAttribute("data-icon"); });
}

const S = {
  init: null, game: null, mode: "machine", preset: null, provider: "deepseek",
  running: false, busy: false, hw: null, stage: null, downloads: {},
  live: { running: false, starting: false, count: 0, last: "", launch: false },
};

/* ---------------- запуск ---------------- */

window.addEventListener("pywebviewready", boot);

async function boot() {
  paintIcons();
  S.init = await api().init();
  const st = S.init.settings;
  $("#ver").textContent = S.init.version;
  $("#aboutVer").textContent = S.init.version;
  $("#tgHandle").textContent = "@" + S.init.order.telegram;
  S.mode = ["machine", "local", "cloud"].includes(st.mode) ? st.mode : "machine";
  S.provider = st.cloud_provider || "deepseek";
  S.preset = st.local_model || null;
  $("#useGpu").checked = st.local_gpu !== false;

  bindUi();
  renderProviders();
  applyProvider(S.provider, true);
  renderMachine();
  renderPresets();
  selectMode(S.mode, true);
  fillSettings();
  loadHardware();
  if (st.game_dir) detectGame(st.game_dir, true);
  updateRunState();
}

/* ---------------- навигация ---------------- */

function showPage(name) {
  if (S.running && name !== "run") name = name === "main" ? "run" : name;
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === name || (name === "run" && b.dataset.page === "main")));
  if (name === "models") renderModels();
  $(".content").scrollTop = 0;
}

function bindUi() {
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
  $("#orderCta").addEventListener("click", openOrder);
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest(".modal").classList.add("hidden")));
  $$(".modal").forEach((m) => m.addEventListener("mousedown", (e) => { if (e.target === m) m.classList.add("hidden"); }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $$(".modal").forEach((m) => m.classList.add("hidden")); });

  // игра
  $("#pickGame").addEventListener("click", pickGame);
  $("#changeGame").addEventListener("click", pickGame);

  // способы
  $$(".mode").forEach((m) => m.addEventListener("click", () => {
    if (m.dataset.mode === "order") return openOrder();
    selectMode(m.dataset.mode);
  }));

  // машинный
  $("#mtDownload").addEventListener("click", async () => {
    const r = await api().download_machine();
    if (!r.ok) return toast(r.error, true);
    startDownloadUi("machine");
  });

  // локальная нейросеть
  $("#useGpu").addEventListener("change", () => api().save_settings({ local_gpu: $("#useGpu").checked }));

  // облако
  $("#cloudUrl").addEventListener("change", () => saveCloud());
  $("#cloudModel").addEventListener("change", () => saveCloud());
  $("#cloudKey").addEventListener("change", storeKey);
  $("#rememberKey").addEventListener("change", storeKey);
  $("#toggleKey").addEventListener("click", () => {
    const k = $("#cloudKey");
    k.type = k.type === "password" ? "text" : "password";
  });
  $("#keyLink").addEventListener("click", (e) => {
    e.preventDefault();
    const p = provider();
    if (p && p.key_url) api().open_url(p.key_url);
  });
  $("#refreshModels").addEventListener("click", refreshModels);
  $("#testCloud").addEventListener("click", testCloud);

  // запуск
  $("#startBtn").addEventListener("click", start);
  $("#restoreBtn").addEventListener("click", () => restoreGame(S.game && S.game.path));
  $("#stopBtn").addEventListener("click", () => {
    api().cancel_task();
    $("#stopBtn").disabled = true;
    $("#progressMsg").textContent = "Останавливаю… (текущий запрос будет завершён)";
  });
  $("#resBack").addEventListener("click", () => { S.running = false; showPage("main"); if (S.game) detectGame(S.game.path, true); });
  $("#resOpen").addEventListener("click", () => S.game && api().open_folder(S.game.path));
  $("#resRestore").addEventListener("click", () => restoreGame(S.game && S.game.path, true));

  // живой перевод
  $("#livePlay").addEventListener("click", async () => {
    if (S.live.running) {
      const r = await api().launch_game(S.game ? S.game.path : "");
      if (!r.ok) toast(r.error, true);
      return;
    }
    S.live.starting = true;
    S.live.launch = true;
    renderLiveBar("Запускаю переводчик…");
    await api().start_live();
  });
  $("#liveStop").addEventListener("click", () => api().stop_live());
  $("#liveDesk").addEventListener("click", async () => {
    const r = await api().desktop_shortcut(S.game ? S.game.path : "");
    toast(r.ok ? "Ярлык «" + r.name + "» создан на рабочем столе" : r.error, !r.ok);
  });

  // заказ
  $("#openTg").addEventListener("click", () => api().open_url(S.init.order.url));
  $("#copyTg").addEventListener("click", () => copyText("@" + S.init.order.telegram));

  // модели и настройки
  $("#clearMem").addEventListener("click", () => confirmBox("Очистить память переводов?",
    "Уже переведённые строки будут переводиться заново при следующих запусках.", async () => {
      await api().clear_memory(); toast("Память переводов очищена"); renderModels();
    }));
  $("#openData").addEventListener("click", () => api().open_data_folder());
  $("#setDataPick").addEventListener("click", async () => {
    const d = await api().pick_folder();
    if (d) { await api().save_settings({ data_dir: d }); await reloadInit(); fillSettings(); toast("Папка данных изменена"); }
  });
  $("#setDataReset").addEventListener("click", async () => { await api().save_settings({ data_dir: "" }); await reloadInit(); fillSettings(); });
  $("#setFontPick").addEventListener("click", async () => {
    const f = await api().pick_file("font");
    if (f) { await api().save_settings({ font_path: f }); S.init.settings.font_path = f; fillSettings(); }
  });
  $("#setFontReset").addEventListener("click", async () => { await api().save_settings({ font_path: "" }); S.init.settings.font_path = ""; fillSettings(); });
  $("#setGgufPick").addEventListener("click", async () => {
    const f = await api().pick_file("gguf");
    if (f) { await api().save_settings({ local_custom_model: f }); S.init.settings.local_custom_model = f; fillSettings(); renderPresets(); }
  });
  $("#setGgufReset").addEventListener("click", async () => { await api().save_settings({ local_custom_model: "" }); S.init.settings.local_custom_model = ""; fillSettings(); renderPresets(); });
  $("#setThreads").addEventListener("change", (e) => api().save_settings({ threads: Math.max(0, +e.target.value || 0) }));
  $("#setParallel").addEventListener("change", (e) => api().save_settings({ cloud_parallel: Math.min(16, Math.max(1, +e.target.value || 4)) }));
}

/* ---------------- игра ---------------- */

async function pickGame() {
  const d = await api().pick_folder();
  if (d) detectGame(d);
}

async function detectGame(path, quiet = false) {
  const r = await api().detect(path);
  if (!r.ok) {
    if (!quiet) toast(r.error, true);
    S.game = null;
    $("#gameEmpty").classList.remove("hidden");
    $("#gameInfo").classList.add("hidden");
    renderLiveBar();
    return updateRunState();
  }
  S.game = { path, ...r };
  $("#gameEmpty").classList.add("hidden");
  $("#gameInfo").classList.remove("hidden");
  $("#gameName").textContent = r.name;
  $("#gamePath").textContent = path;
  const chips = [`<span class="chip accent">${icon("gamepad")}${esc(r.engine)}</span>`];
  if (r.russified) chips.push(`<span class="chip ok">${icon("check")}Уже русифицирована</span>`);
  if (r.progress && r.progress.total && !r.russified) {
    const pct = Math.round((r.progress.done / r.progress.total) * 100);
    if (pct > 0 && pct < 100) chips.push(`<span class="chip warn">${icon("pause")}Переведено ${pct}% — можно продолжить</span>`);
  }
  $("#gameChips").innerHTML = chips.join("");
  $("#gameNotes").innerHTML = (r.notes || []).map((n) => `<div class="note">${icon("info")}<span>${esc(n)}</span></div>`).join("");
  updateRunState();
  renderLiveBar();
}

/* ---------------- способы перевода ---------------- */

function selectMode(mode, silent = false) {
  S.mode = mode;
  $$(".mode").forEach((m) => m.classList.toggle("active", m.dataset.mode === mode));
  ["machine", "local", "cloud"].forEach((m) => $("#panel-" + m).classList.toggle("hidden", m !== mode));
  if (!silent) api().save_settings({ mode });
  updateRunState();
}

function renderMachine() {
  const m = S.init.machine;
  const dl = S.downloads.machine;
  $("#mtDot").className = "dot " + (m.installed ? "ok" : "warn");
  $("#mtText").textContent = m.installed
    ? `Установлена (${m.size_mb} МБ) — работает офлайн.`
    : `Не скачана. Скачается автоматически при первом запуске (~${m.download_mb} МБ).`;
  $("#mtDownload").classList.toggle("hidden", m.installed);
  $("#mtDownload").disabled = !!dl;
}

async function loadHardware() {
  S.hw = await api().hardware();
  const g = S.hw.gpus || [];
  let text = `ОЗУ ${Math.round(S.hw.ram_gb)} ГБ`;
  if (g.length) text += " · " + g.map((x) => `${x.name} (${Math.round(x.vram_gb)} ГБ)`).join(", ");
  else if (!S.hw.gpu_known) text += " · видеокарта определится после установки движка нейросети";
  else text += " · видеокарта не найдена — нейросеть будет работать на процессоре";
  $("#hwText").textContent = "Ваш ПК: " + text;
  if (!S.preset) S.preset = S.hw.recommended;
  renderPresets();
}

function renderPresets() {
  const custom = S.init.settings.local_custom_model;
  const rec = S.hw ? S.hw.recommended : null;
  const box = $("#presets");
  if (custom) {
    box.innerHTML = `<div class="preset active"><span class="radio"></span><div class="grow">
      <div class="preset-title"><b>Своя модель</b><span class="chip">GGUF</span></div>
      <div class="muted mono">${esc(custom)}</div></div>
      <div class="preset-side"><span class="muted small">изменить — в настройках</span></div></div>`;
    return;
  }
  box.innerHTML = S.init.presets.map((p) => {
    const dl = S.downloads["local:" + p.id];
    const status = p.installed
      ? `<span class="chip ok">${icon("check")}Скачана</span>`
      : `<button class="btn sm" data-dl="${p.id}" ${dl || S.busy ? "disabled" : ""}>${icon("download")}${p.size_gb.toFixed(1)} ГБ</button>`;
    return `<div class="preset ${S.preset === p.id ? "active" : ""}" data-preset="${p.id}">
      <span class="radio"></span>
      <div class="grow">
        <div class="preset-title"><b>${esc(p.title)}</b>${rec === p.id ? '<span class="chip accent">рекомендуется для вашего ПК</span>' : ""}</div>
        <div class="muted">${esc(p.description)}</div>
      </div>
      <div class="preset-side">${status}<span class="muted small">от ${p.min_ram_gb} ГБ ОЗУ</span></div>
    </div>`;
  }).join("");
  $$(".preset[data-preset]", box).forEach((el) => el.addEventListener("click", (e) => {
    if (e.target.closest("[data-dl]")) return;
    S.preset = el.dataset.preset;
    api().save_settings({ local_model: S.preset });
    renderPresets();
  }));
  $$("[data-dl]", box).forEach((b) => b.addEventListener("click", async () => {
    S.preset = b.dataset.dl;
    api().save_settings({ local_model: S.preset });
    const r = await api().download_local(b.dataset.dl);
    if (!r.ok) return toast(r.error, true);
    startDownloadUi("local:" + b.dataset.dl);
  }));
}

/* ---------------- облако ---------------- */

const provider = () => S.init.providers.find((p) => p.id === S.provider);

function renderProviders() {
  $("#providers").innerHTML = S.init.providers.map((p) =>
    `<button class="prov ${p.id === S.provider ? "active" : ""}" data-prov="${p.id}">${esc(p.title)}${S.init.saved_keys[p.id] ? '<span class="saved">●</span>' : ""}</button>`).join("");
  $$("[data-prov]").forEach((b) => b.addEventListener("click", () => applyProvider(b.dataset.prov)));
}

function applyProvider(id, fromSettings = false) {
  S.provider = id;
  const p = provider() || S.init.providers[0];
  const st = S.init.settings;
  const same = fromSettings && st.cloud_provider === id;
  $("#cloudUrl").value = same && st.cloud_base_url ? st.cloud_base_url : p.base_url;
  $("#cloudModel").value = same && st.cloud_model ? st.cloud_model : p.model;
  $("#cloudKey").value = "";
  $("#cloudKey").placeholder = S.init.saved_keys[id] ? "•••••••• ключ сохранён (введите новый, чтобы заменить)" : (p.needs_key ? "вставьте ключ" : "не нужен");
  $("#keyLink").classList.toggle("hidden", !p.key_url);
  $("#keyLink").textContent = p.needs_key ? "где взять ключ" : "сайт";
  $("#providerNote").textContent = p.note || "";
  $("#modelList").innerHTML = "";
  $("#modelHint").textContent = "Нажмите ⟳, чтобы загрузить актуальный список моделей провайдера.";
  $("#testResult").textContent = "";
  $$(".prov").forEach((b) => b.classList.toggle("active", b.dataset.prov === id));
  if (!fromSettings) saveCloud();
}

function saveCloud() {
  S.init.settings.cloud_provider = S.provider;
  S.init.settings.cloud_base_url = $("#cloudUrl").value.trim();
  S.init.settings.cloud_model = $("#cloudModel").value.trim();
  api().save_settings({ cloud_provider: S.provider, cloud_base_url: S.init.settings.cloud_base_url, cloud_model: S.init.settings.cloud_model });
  updateRunState();
}

async function storeKey() {
  const key = $("#cloudKey").value.trim();
  const remember = $("#rememberKey").checked;
  if (!key && !S.init.saved_keys[S.provider]) return;
  if (!key && remember) return;
  const r = await api().set_api_key(S.provider, key, remember);
  S.init.saved_keys[S.provider] = r.saved;
  renderProviders();
  updateRunState();
}

async function refreshModels() {
  const btn = $("#refreshModels");
  btn.classList.add("spin");
  const r = await api().list_models(S.provider, $("#cloudUrl").value.trim(), $("#cloudKey").value.trim());
  btn.classList.remove("spin");
  if (!r.ok) { $("#modelHint").textContent = "Не удалось получить список: " + r.error; return; }
  $("#modelList").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
  const cur = $("#cloudModel").value.trim();
  if (r.models.length && (!cur || !r.models.includes(cur))) {
    $("#cloudModel").value = r.models[0];
    saveCloud();
  }
  $("#modelHint").textContent = r.models.length
    ? `Доступно моделей: ${r.models.length}. Рекомендуем: ${r.models.slice(0, 3).join(", ")}`
    : "Провайдер не вернул список моделей — впишите имя вручную.";
}

async function testCloud() {
  const out = $("#testResult");
  out.className = "test-result muted";
  out.textContent = "Проверяю…";
  await storeKey();
  const r = await api().test_cloud(S.provider, $("#cloudUrl").value.trim(), $("#cloudModel").value.trim(), $("#cloudKey").value.trim());
  out.className = "test-result " + (r.ok ? "ok" : "err");
  out.textContent = r.ok ? "Работает: «" + r.sample + "»" : r.error;
}

/* ---------------- запуск ---------------- */

function updateRunState() {
  const ready = !!S.game && !S.busy;
  let hint = "Выберите папку с игрой.";
  let can = ready;
  if (S.game) {
    if (S.mode === "cloud") {
      const p = provider();
      const hasKey = S.init.saved_keys[S.provider] || $("#cloudKey").value.trim();
      if (p && p.needs_key && !hasKey) { hint = "Укажите API-ключ провайдера."; can = false; }
      else if (!$("#cloudModel").value.trim()) { hint = "Укажите модель."; can = false; }
      else hint = `Перевод через ${p ? p.title : "API"}, модель ${$("#cloudModel").value.trim()}.`;
    } else if (S.mode === "local") {
      const p = S.init.presets.find((x) => x.id === S.preset);
      hint = S.init.settings.local_custom_model ? "Перевод своей моделью GGUF на этом ПК."
        : p ? `Перевод нейросетью ${p.title.split(" —")[0]} на этом ПК` + (p.installed ? "." : ` — модель скачается автоматически (${p.size_gb.toFixed(1)} ГБ).`)
          : "Выберите модель.";
    } else {
      hint = "Машинный перевод офлайн" + (S.init.machine.installed ? "." : ` — модель скачается автоматически (~${S.init.machine.download_mb} МБ).`);
    }
    if (S.game.russified) hint += " Игра уже русифицирована — перевод будет обновлён.";
  }
  $("#runHint").textContent = hint;
  $("#startBtn").disabled = !can;
  $("#restoreBtn").disabled = !(S.game && S.game.russified) || S.busy;
}

async function start() {
  if (!S.game) return;
  await storeKey();
  const opts = {
    game_dir: S.game.path, mode: S.mode, local_model: S.preset || "", local_gpu: $("#useGpu").checked,
    cloud_provider: S.provider, cloud_base_url: $("#cloudUrl").value.trim(), cloud_model: $("#cloudModel").value.trim(),
    api_key: $("#cloudKey").value.trim(),
  };
  const r = await api().start(opts);
  if (!r.ok) return toast(r.error, true);
  S.running = true;
  S.busy = true;
  S.stage = null;
  $("#runGame").textContent = S.game.name;
  const modeTitle = { machine: "машинный перевод", local: "нейросеть на ПК", cloud: "облачная нейросеть" }[S.mode];
  $("#runSub").textContent = `${S.game.engine} · ${modeTitle}`;
  $("#stepper").innerHTML = STAGES.map(([id, t]) => `<li data-stage="${id}"><span class="s"></span><span class="t">${t}</span></li>`).join("");
  $("#log").innerHTML = "";
  $("#resultCard").classList.add("hidden");
  $("#progressCard").classList.remove("hidden");
  $("#runFoot").classList.remove("hidden");
  $("#stopBtn").disabled = false;
  setProgress(null, "Запуск…", "");
  showPage("run");
}

function setProgress(fraction, title, msg) {
  const bar = $("#progressFill").parentElement;
  if (title != null) $("#progressTitle").textContent = title;
  if (msg != null) $("#progressMsg").textContent = msg;
  if (fraction == null) {
    bar.classList.add("indeterminate");
    $("#progressPct").textContent = "";
  } else {
    bar.classList.remove("indeterminate");
    const pct = Math.max(0, Math.min(100, fraction * 100));
    $("#progressFill").style.width = pct + "%";
    $("#progressPct").textContent = Math.floor(pct) + "%";
  }
}

function log(msg, cls = "") {
  const box = $("#log");
  const line = document.createElement("div");
  if (cls) line.className = cls;
  const t = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  line.textContent = `${t}  ${msg}`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function markStage(id, state) {
  $$("#stepper li").forEach((li) => {
    const idx = STAGES.findIndex((s) => s[0] === li.dataset.stage);
    const cur = STAGES.findIndex((s) => s[0] === id);
    li.classList.remove("active", "done", "fail");
    if (idx < cur) li.classList.add("done");
    else if (idx === cur) li.classList.add(state);
  });
}

/* ---------------- события Python ---------------- */

window.onBackendEvent = (ev) => {
  switch (ev.type) {
    case "stage": {
      S.stage = ev.id;
      markStage(ev.id, "active");
      setProgress(null, ev.title, "");
      log(ev.title, "s");
      break;
    }
    case "progress": {
      const f = ev.total ? ev.done / ev.total : 1;
      setProgress(f, "Перевод", ev.message);
      break;
    }
    case "status": {
      setProgress(ev.fraction == null ? null : ev.fraction, null, ev.message);
      break;
    }
    case "log": {
      log(ev.message, ev.level === "warning" ? "w" : ev.level === "error" ? "e" : "");
      break;
    }
    case "run_finished": finishRun(ev); break;
    case "live_status": onLiveStatus(ev); break;
    case "live": {
      S.live.count = ev.count;
      S.live.last = ev.translation;
      renderLiveBar();
      break;
    }
    case "download": onDownload(ev); break;
    case "task_error": {
      S.busy = false;
      toast(ev.message, true);
      updateRunState();
      break;
    }
  }
};

function finishRun(ev) {
  S.busy = false;
  const ok = ev.success;
  const errs = ev.errors || [];
  if (S.game && ev.injected) {
    S.game.russified = true;
    S.game.live = !!ev.live;
    renderLiveBar();
  }
  if (ok) markStage("__end__", "done"), $$("#stepper li").forEach((li) => li.classList.add("done"));
  else if (S.stage) markStage(S.stage, ev.cancelled ? "active" : "fail");
  $$("#stepper li.active").forEach((li) => li.classList.remove("active"));
  $("#progressCard").classList.add("hidden");
  $("#runFoot").classList.add("hidden");
  const card = $("#resultCard");
  card.classList.remove("hidden");
  const st = ev.stats || {};
  let kind = ok ? (st.failed ? "warn" : "ok") : ev.cancelled ? "warn" : "err";
  $("#resultIco").className = "result-ico " + kind;
  $("#resultIco").innerHTML = icon(kind === "ok" ? "check-circle" : kind === "warn" ? "alert" : "x-circle");
  $("#resultTitle").textContent = ok ? "Игра русифицирована!" : ev.cancelled ? "Остановлено" : "Не получилось";
  $("#resultText").textContent = ok
    ? "Запускайте игру. Если что-то не так — откат вернёт всё как было."
    : ev.cancelled ? "Прогресс перевода сохранён — при следующем запуске продолжим с того же места."
      : "Игра не изменена. Подробности ниже и в журнале.";
  $("#resultStats").innerHTML = st.total ? [
    ["Строк найдено", st.total], ["Переведено", st.translated], ["Из памяти", st.memory || 0], ["Не переведено", st.failed],
  ].map(([t, v]) => `<div class="stat"><b>${v ?? 0}</b><span>${t}</span></div>`).join("") : "";
  const msgs = [
    ...errs.map((m) => ["e", "x-circle", m]),
    ...(ev.instructions || []).map((m) => ["n", "info", m]),
    ...(ev.warnings || []).slice(0, 12).map((m) => ["w", "alert", m]),
  ];
  $("#resultMsgs").innerHTML = msgs.map(([c, i, m]) => `<div class="msg ${c}">${icon(i)}<span>${esc(m)}</span></div>`).join("");
  $("#resRestore").classList.toggle("hidden", !ev.injected);
  errs.forEach((e) => log(e, "e"));
  log(ok ? "Готово" : ev.cancelled ? "Остановлено" : "Завершено с ошибкой", ok ? "s" : "e");
  reloadInit();
}

/* ---------------- живой перевод ---------------- */

function renderLiveBar(message) {
  const L = S.live;
  const show = (S.game && S.game.live && S.game.russified) || L.running || L.starting;
  $("#liveBar").classList.toggle("hidden", !show);
  if (!show) return;
  $("#liveDot").className = "live-dot " + (L.running ? "on" : L.starting ? "wait" : "");
  $("#liveStop").classList.toggle("hidden", !L.running && !L.starting);
  $("#liveDesk").classList.toggle("hidden", L.running || L.starting || !(S.game && S.game.russified));
  $("#livePlay").innerHTML = icon("play") + (L.running ? "Запустить игру" : "Играть с живым переводом");
  $("#livePlay").disabled = L.starting;
  let text;
  if (message) text = message;
  else if (L.running) text = L.count
    ? `Включён · переведено на лету: ${L.count}` + (L.last ? ` · «${L.last.slice(0, 60)}»` : "")
    : "Включён — запустите игру, новый текст будет переводиться на лету.";
  else text = "Текст, который игра собирает на лету, переводится прямо в игре. Запускайте её отсюда " +
    "или ярлыком «Играть на русском» в папке игры — программу держать открытой не нужно.";
  $("#liveText").textContent = text;
}

async function onLiveStatus(ev) {
  const L = S.live;
  if (ev.error) {
    L.starting = false; L.running = false; L.launch = false;
    toast(ev.error, true);
  } else if (ev.running === true) {
    L.starting = false; L.running = true;
    if (L.launch) {
      L.launch = false;
      const r = await api().launch_game(S.game ? S.game.path : "");
      if (!r.ok) toast(r.error, true);
    }
  } else if (ev.running === false) {
    L.starting = false; L.running = false;
  }
  renderLiveBar(L.starting ? ev.message : undefined);
}

/* ---------------- загрузки ---------------- */

function startDownloadUi(task) {
  S.downloads[task] = true;
  S.busy = true;
  const box = task === "machine" ? $("#mtProgress") : $("#localProgress");
  box.classList.remove("hidden");
  box.querySelector(".bar").classList.add("indeterminate");
  box.querySelector(".dl-text").textContent = "Начинаю загрузку…";
  renderMachine(); renderPresets(); updateRunState();
  if ($("#page-models").classList.contains("active")) renderModels();
}

function onDownload(ev) {
  const box = ev.task === "machine" ? $("#mtProgress") : $("#localProgress");
  const bar = box.querySelector(".bar");
  box.classList.remove("hidden");
  if (ev.fraction != null) {
    bar.classList.remove("indeterminate");
    bar.querySelector(".bar-fill").style.width = (ev.fraction * 100).toFixed(1) + "%";
  } else if (!ev.done && !ev.error) {
    bar.classList.add("indeterminate");
  }
  box.querySelector(".dl-text").textContent = ev.error ? "Ошибка: " + ev.error : ev.message || "";
  const modelsRow = $(`#models-dl`);
  if (modelsRow) modelsRow.textContent = ev.error ? "Ошибка: " + ev.error : ev.message || "";
  if (ev.done || ev.error) {
    delete S.downloads[ev.task];
    S.busy = false;
    if (ev.done) { toast(ev.message || "Готово"); setTimeout(() => box.classList.add("hidden"), 1500); }
    else if (ev.error !== "Отменено.") toast(ev.error, true);
    reloadInit().then(() => { loadHardware(); if ($("#page-models").classList.contains("active")) renderModels(); });
  }
}

async function reloadInit() {
  const keep = S.init.settings;
  S.init = await api().init();
  S.init.settings = { ...keep, ...S.init.settings };
  renderMachine();
  renderPresets();
  updateRunState();
}

/* ---------------- модели ---------------- */

async function renderModels() {
  const r = await api().models_overview();
  $("#dataDir").textContent = r.data_dir;
  $("#memInfo").textContent = (r.memory_bytes ? `Занимает ${size(r.memory_bytes)}. ` : "") +
    "Уже переведённые строки — повторный запуск и другие игры не тратят на них время и деньги.";
  const busy = S.busy;
  $("#modelsList").innerHTML = r.items.map((m) => {
    const ico = m.kind === "machine" ? "cpu" : m.kind === "runtime" ? "zap" : "chip";
    const state = m.installed ? `Скачана · ${size(m.bytes)}` : m.download_gb ? `Не скачана · ${m.download_gb.toFixed(1)} ГБ` : "Не скачана";
    const action = m.installed
      ? `<button class="btn ghost sm" data-del="${m.kind}:${m.id}" ${busy ? "disabled" : ""}>${icon("trash")}Удалить</button>`
      : m.kind !== "runtime" ? `<button class="btn sm" data-get="${m.kind}:${m.id}" ${busy ? "disabled" : ""}>${icon("download")}Скачать</button>` : "";
    return `<div class="list-row"><span class="i big">${svg(ico)}</span>
      <div class="grow"><b>${esc(m.title)}</b><div class="muted small">${state}</div></div>${action}</div>`;
  }).join("") + (busy ? `<div class="list-row"><div class="grow muted small" id="models-dl">Идёт загрузка…</div>
      <button class="btn ghost sm" id="cancelDl">${icon("x")}Отменить</button></div>` : "");
  $$("[data-del]").forEach((b) => b.addEventListener("click", () => {
    const [kind, id] = b.dataset.del.split(":");
    confirmBox("Удалить модель?", "Файлы модели будут удалены с диска. Скачать её можно снова в любой момент.", async () => {
      const res = await api().delete_model(kind, id);
      if (!res.ok) toast(res.error, true);
      await reloadInit(); renderModels();
    });
  }));
  $$("[data-get]").forEach((b) => b.addEventListener("click", async () => {
    const [kind, id] = b.dataset.get.split(":");
    const res = kind === "machine" ? await api().download_machine() : await api().download_local(id);
    if (!res.ok) return toast(res.error, true);
    startDownloadUi(kind === "machine" ? "machine" : "local:" + id);
  }));
  const c = $("#cancelDl");
  if (c) c.addEventListener("click", () => api().cancel_task());
}

/* ---------------- настройки ---------------- */

function fillSettings() {
  const st = S.init.settings;
  $("#setDataDir").textContent = S.init.data_dir;
  $("#setFont").textContent = st.font_path || "Встроенный PT Sans";
  $("#setThreads").value = st.threads || 0;
  $("#setParallel").value = st.cloud_parallel || 4;
  $("#setGguf").textContent = st.local_custom_model || "Не используется — Gemma 4 из списка моделей";
}

/* ---------------- откат ---------------- */

function restoreGame(path, fromResult = false) {
  if (!path) return;
  confirmBox("Откатить русификацию?", "Игре вернутся оригинальные файлы, все изменения русификатора будут удалены. Перевод сохранится в программе — повторная русификация будет быстрой.", async () => {
    const r = await api().restore(path);
    toast((r.notes || []).join(" "), !r.ok);
    if (fromResult) { S.running = false; showPage("main"); }
    detectGame(path, true);
  });
}

/* ---------------- заказ перевода ---------------- */

function openOrder() {
  $("#orderModal").classList.remove("hidden");
}

/* ---------------- мелочи ---------------- */

function confirmBox(title, text, onOk) {
  $("#confirmTitle").textContent = title;
  $("#confirmText").textContent = text;
  const m = $("#confirmModal");
  m.classList.remove("hidden");
  const ok = $("#confirmOk");
  const handler = () => { m.classList.add("hidden"); ok.removeEventListener("click", handler); onOk(); };
  ok.replaceWith(ok.cloneNode(true));
  $("#confirmOk").addEventListener("click", handler);
}

let toastTimer = null;
function toast(text, isError = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 3000);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("Скопировано: " + text);
}
