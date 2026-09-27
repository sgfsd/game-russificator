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
  package: '<path d="M21 8 12 3 3 8v8l9 5 9-5Z"/><path d="m3 8 9 5 9-5M12 13v8"/><path d="m7.5 5.5 9 5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  live: '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4M19.1 4.9a10 10 0 0 1 0 14.2M4.9 19.1a10 10 0 0 1 0-14.2"/>',
  power: '<path d="M12 3v9"/><path d="M6.3 7.3a8 8 0 1 0 11.4 0"/>',
  "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
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
  lib: { games: [], filter: "all", search: "", loaded: false, scanning: false, scannedAt: 0, current: null },
  imp: null,
  liveTimer: null, liveSt: null,
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
  api().overlay_status().then((x) => { S.liveSt = x; $("#navLiveDot").classList.toggle("hidden", !x.running); });
}

/* ---------------- навигация ---------------- */

function showPage(name) {
  if (S.running && name !== "run") name = name === "main" ? "run" : name;
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === name || (name === "run" && b.dataset.page === "main")));
  if (name === "models") renderModels();
  if (name === "games") loadLibrary(false);
  if (name === "settings") renderLibFolders();
  if (name === "live") startLivePolling(); else stopLivePolling();
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

  // мои игры
  $("#pickFromLib").addEventListener("click", () => showPage("games"));
  $("#changeFromLib").addEventListener("click", () => showPage("games"));
  $("#gameSearch").addEventListener("input", (e) => { S.lib.search = e.target.value.trim().toLowerCase(); renderGames(); });
  $$("#gameFilters .filter").forEach((b) => b.addEventListener("click", () => {
    S.lib.filter = b.dataset.filter;
    $$("#gameFilters .filter").forEach((x) => x.classList.toggle("active", x === b));
    renderGames();
  }));
  $("#libRefresh").addEventListener("click", () => loadLibrary(true));
  $("#libAdd").addEventListener("click", async () => {
    const r = await api().library_add("");
    if (r.ok) { upsertGame(r.game); renderGames(); openGame(r.game); }
    else if (r.error) toast(r.error, true);
  });
  $("#libImport").addEventListener("click", () => openImport(""));
  $("#setLibAdd").addEventListener("click", async () => {
    const r = await api().library_add_folder();
    if (r.ok) { renderLibFolders(); toast("Папка добавлена — ищу игры"); }
  });
  $("#imPick").addEventListener("click", async () => {
    const d = await api().pick_folder();
    if (!d || !S.imp) return;
    S.imp.game = d;
    $("#imGame").textContent = d;
    const r = await api().import_check(S.imp.path, d);
    setImportCheck(r.ok, r.exact, r.message || r.error);
  });
  $("#imGo").addEventListener("click", runImport);

  // живой перевод
  $("#livePower").addEventListener("click", async () => {
    const on = S.liveSt && S.liveSt.running;
    const r = on ? await api().overlay_stop() : await api().overlay_start();
    if (!r.ok) return toast(r.error, true);
    $("#liveSub").textContent = on ? "Выключаю…" : "Запускаю…";
    setTimeout(refreshLive, 900);
  });
  $$("[data-live]").forEach((b) => b.addEventListener("click", async () => {
    const r = await api().overlay_cmd(b.dataset.live);
    if (!r.ok) toast(r.error || "Живой перевод не запущен", true);
    else if (b.dataset.live.startsWith("mark")) toast("Игра больше не будет переводиться");
    refreshLive();
  }));
  $$("#liveMode button").forEach((b) => b.addEventListener("click", async () => {
    await api().overlay_settings({ live_mode: b.dataset.mode });
    S.liveSt.settings.live_mode = b.dataset.mode;
    renderLiveSettings();
  }));
  const liveRange = (id, key) => $(id).addEventListener("input", (e) => {
    const v = +e.target.value / 100;
    S.liveSt.settings[key] = v;
    renderLiveSettings();
    clearTimeout(S["t_" + key]);
    S["t_" + key] = setTimeout(() => api().overlay_settings({ [key]: v }), 250);
  });
  liveRange("#liveScale", "live_font_scale");
  liveRange("#liveOpacity", "live_opacity");
  $("#liveAutostart").addEventListener("change", async (e) => {
    const r = await api().overlay_autostart(e.target.checked);
    if (!r.ok) { toast(r.error, true); e.target.checked = !e.target.checked; }
  });
  $("#liveOcrInstall").addEventListener("click", async () => {
    const r = await api().overlay_install_ocr();
    toast(r.ok ? "Идёт установка в окне PowerShell — дождитесь «Готово» и нажмите «Проверить снова»" : r.error, !r.ok);
  });
  $("#liveOcrRetry").addEventListener("click", async () => {
    $("#liveOcrText").textContent = "Перезапускаю живой перевод…";
    await api().overlay_restart();
    setTimeout(refreshLive, 2500);
  });

  // архив-русификатор
  $("#exportBtn").addEventListener("click", () => openExport());
  $("#resExport").addEventListener("click", () => openExport());
  $("#exportPick").addEventListener("click", async () => {
    const d = await api().pick_export_dir();
    if (d) $("#exportDir").textContent = d;
  });
  $("#exportGo").addEventListener("click", runExport);
  $("#exportReveal").addEventListener("click", () => S.exportPath && api().reveal(S.exportPath));

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
    return updateRunState();
  }
  S.game = { path, ...r };
  $("#gameEmpty").classList.add("hidden");
  $("#gameInfo").classList.remove("hidden");
  $("#gameName").textContent = r.name;
  $("#gamePath").textContent = path;
  const chips = [`<span class="chip accent">${icon("gamepad")}${esc(r.engine)}</span>`];
  if (r.russified && r.intact === false) chips.push(`<span class="chip warn">${icon("alert")}Перевод слетел после обновления игры</span>`);
  else if (r.russified) chips.push(`<span class="chip ok">${icon("check")}Уже русифицирована</span>`);
  if (r.progress && r.progress.total && !r.russified) {
    const pct = Math.round((r.progress.done / r.progress.total) * 100);
    if (pct > 0 && pct < 100) chips.push(`<span class="chip warn">${icon("pause")}Переведено ${pct}% — можно продолжить</span>`);
  }
  $("#gameChips").innerHTML = chips.join("");
  $("#gameNotes").innerHTML = (r.notes || []).map((n) => `<div class="note">${icon("info")}<span>${esc(n)}</span></div>`).join("");
  updateRunState();
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
    if (S.game.russified && S.game.intact === false) hint += " Игра обновилась — перевод поставится заново, готовые строки возьмутся из памяти.";
    else if (S.game.russified) hint += " Игра уже русифицирована — перевод будет обновлён.";
  }
  $("#runHint").textContent = hint;
  $("#startBtn").disabled = !can;
  $("#restoreBtn").disabled = !(S.game && S.game.russified) || S.busy;
  const exp = $("#exportBtn");
  exp.disabled = !(S.game && S.game.can_export) || S.busy;
  exp.title = S.game && !S.game.can_export && S.game.export_reason ? S.game.export_reason
    : "Архив с установщиком — отправьте другу";
}

async function start() {
  if (!S.game) return;
  await storeKey();
  const opts = {
    game_dir: S.game.path, mode: S.mode, local_model: S.preset || "", local_gpu: $("#useGpu").checked,
    cloud_provider: S.provider, cloud_base_url: $("#cloudUrl").value.trim(), cloud_model: $("#cloudModel").value.trim(),
    api_key: $("#cloudKey").value.trim(), reuse: !!(S.game.russified && S.game.intact === false) || !!S.reuseNext,
  };
  S.reuseNext = false;
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
    case "library_progress": if (S.lib.scanning) setLibStatus(ev.message, true); break;
    case "library": onLibrary(ev); break;
    case "import": onImportProgress(ev); break;
    case "import_done": onImportDone(ev); break;
    case "download": onDownload(ev); break;
    case "export": onExportProgress(ev); break;
    case "export_done": onExportDone(ev); break;
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
    S.game.intact = true;
    S.game.live = !!ev.live;
    if (S.lib.loaded) api().game_status(S.game.path).then((g) => { if (g && g.path) { upsertGame(g); renderGames(); } });
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
  $("#resExport").classList.toggle("hidden", !ok);
  if (ok && S.game) { S.game.can_export = true; S.game.russified = true; }
  errs.forEach((e) => log(e, "e"));
  log(ok ? "Готово" : ev.cancelled ? "Остановлено" : "Завершено с ошибкой", ok ? "s" : "e");
  reloadInit();
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

/* ---------------- мои игры ---------------- */

async function loadLibrary(refresh) {
  if (S.lib.loaded && !refresh) { renderGames(); return; }
  const r = await api().library(!!refresh);
  S.lib.games = r.games || [];
  S.lib.scannedAt = r.scanned_at || 0;
  S.lib.scanning = !!r.scanning;
  S.lib.loaded = true;
  if (S.lib.scanning) setLibStatus(S.lib.games.length ? "Обновляю список игр…" : "Ищу игры на компьютере…", true);
  else setLibStatus(libSummary());
  renderGames();
}

function onLibrary(ev) {
  S.lib.games = ev.games || [];
  S.lib.scanning = false;
  S.lib.scannedAt = ev.scanned_at || 0;
  S.lib.loaded = true;
  setLibStatus(ev.error ? "Поиск прервался: " + ev.error : libSummary());
  renderGames();
}

function libSummary() {
  const n = S.lib.games.length;
  if (!n) return "Игры не найдены.";
  const when = S.lib.scannedAt ? new Date(S.lib.scannedAt * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "";
  return `Найдено игр: ${n}` + (when ? ` · список обновлён ${when}` : "");
}

function setLibStatus(text, busy = false) {
  $("#libStatus").innerHTML = (busy ? '<span class="spinner"></span>' : "") + `<span>${esc(text)}</span>`;
  $("#libRefresh").classList.toggle("spin", busy);
}

function gameKind(g) {
  if (g.russified) return "russified";
  if (g.supported) return "supported";
  return "overlay";
}

function upsertGame(g) {
  const i = S.lib.games.findIndex((x) => x.path === g.path);
  if (i >= 0) S.lib.games[i] = { ...S.lib.games[i], ...g };
  else S.lib.games.push(g);
}

function renderGames() {
  const all = S.lib.games;
  const counts = { all: all.length, supported: 0, russified: 0, overlay: 0 };
  all.forEach((g) => { counts[gameKind(g)]++; });
  counts.supported += counts.russified;
  $$("[data-cnt]").forEach((el) => { el.textContent = counts[el.dataset.cnt] ? " " + counts[el.dataset.cnt] : ""; });
  const q = S.lib.search;
  const list = all.filter((g) => {
    if (q && !String(g.title).toLowerCase().includes(q) && !String(g.path).toLowerCase().includes(q)) return false;
    const k = gameKind(g);
    if (S.lib.filter === "supported") return g.supported || g.russified;
    if (S.lib.filter === "russified") return k === "russified";
    if (S.lib.filter === "overlay") return k === "overlay";
    return true;
  });
  $("#gamesGrid").innerHTML = list.map(cardHtml).join("");
  const empty = !list.length && !(S.lib.scanning && !all.length);
  $("#gamesEmpty").classList.toggle("hidden", !empty);
  if (empty) {
    $("#gamesEmptyTitle").textContent = all.length ? "Ничего не найдено" : "Игры не найдены";
    $("#gamesEmptyText").textContent = all.length ? "Попробуйте другой запрос или фильтр."
      : "Добавьте папку игры вручную или укажите папки для поиска в настройках.";
  }
  $$("#gamesGrid .gcard").forEach((el) => {
    el.addEventListener("click", () => openGame(S.lib.games.find((g) => g.path === el.dataset.path)));
    const img = el.querySelector("img");
    if (img) img.addEventListener("error", () => coverFallback(img, el.dataset.appid, el.dataset.title), { once: true });
  });
}

function hue(s) {
  let h = 0;
  for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function initials(t) {
  const words = String(t).replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((words[0] || "?")[0] + (words[1] ? words[1][0] : "")).toUpperCase();
}

function placeholder(g) {
  return `<div class="gph" style="--h:${hue(g.title)}">${esc(initials(g.title))}<small>${esc(g.engine_title || "")}</small></div>`;
}

async function coverFallback(img, appid, title) {
  // нет интернета — обложка из кэша Steam; нет и её — заглушка с инициалами (цвет — от названия)
  const data = appid ? await api().game_cover(appid) : "";
  if (data) { img.src = data; return; }
  const g = S.lib.games.find((x) => x.title === title) || { title, engine_title: "" };
  const box = document.createElement("div");
  box.innerHTML = placeholder(g);
  img.replaceWith(box.firstChild);
}

function statusBadge(g) {
  if (g.russified && g.intact === false) return `<span class="gstatus warn">${icon("alert")}Перевод слетел</span>`;
  if (g.russified) return `<span class="gstatus ok">${icon("check")}На русском</span>`;
  if (!g.supported) return `<span class="gstatus overlay">${icon("live")}Оверлей</span>`;
  return "";
}

function cardHtml(g) {
  const cover = g.cover ? `<img src="${esc(g.cover)}" alt="" loading="lazy">` : placeholder(g);
  const src = (g.sources || [g.source]).map((x) => ({ steam: "Steam", gog: "GOG", epic: "Epic", ubisoft: "Ubisoft", itch: "itch.io", xbox: "Game Pass", registry: "", folder: "", manual: "вручную", russified: "" }[x] ?? x)).filter(Boolean);
  return `<div class="gcard" data-path="${esc(g.path)}" data-appid="${esc(g.appid || "")}" data-title="${esc(g.title)}" title="${esc(g.path)}">
    <div class="gcover" style="--h:${hue(g.title)}">${cover}${statusBadge(g)}</div>
    <div class="gbody">
      <div class="gtitle">${esc(g.title)}</div>
      <div class="gmeta"><span class="chip ${g.supported ? "accent" : ""}">${esc(g.engine_title || "?")}</span><span class="src">${esc(src.join(" · "))}</span></div>
    </div>
  </div>`;
}

function openGame(g) {
  if (!g) return;
  S.lib.current = g;
  const m = $("#gameModal");
  const cover = $("#gmCover");
  cover.innerHTML = g.cover ? `<img src="${esc(g.cover)}" alt="">` : placeholder(g);
  cover.style.setProperty("--h", hue(g.title));
  const img = cover.querySelector("img");
  if (img) img.addEventListener("error", () => coverFallback(img, g.appid, g.title), { once: true });
  $("#gmTitle").textContent = g.title;
  $("#gmPath").textContent = g.path;
  const srcNames = { steam: "Steam", gog: "GOG", epic: "Epic Games", ubisoft: "Ubisoft Connect", itch: "itch.io", xbox: "Game Pass / Microsoft Store", registry: "Установлена в Windows", folder: "Папка поиска", manual: "Добавлена вручную", russified: "Переводилась в программе" };
  $("#gmChips").innerHTML = [`<span class="chip ${g.supported ? "accent" : ""}">${icon("gamepad")}${esc(g.engine_title || "Движок не определён")}</span>`]
    .concat((g.sources || []).map((x) => `<span class="chip">${esc(srcNames[x] || x)}</span>`)).join("");

  const st = [];
  if (g.russified && g.intact === false) {
    st.push(["warn", "alert", "Перевод слетел", "Игра обновилась или Steam проверил целостность файлов. Нажмите «Переустановить перевод» — готовые строки возьмутся из памяти, переведутся только новые."]);
  } else if (g.russified) {
    const lines = g.total ? `Переведено ${g.translated ?? "?"} из ${g.total} строк` : "Игра русифицирована";
    const parts = [g.translator, g.date].filter(Boolean).join(" · ");
    st.push(["ok", "check-circle", g.from_package ? "Установлен русификатор из архива" : "Игра на русском", lines + (parts ? " · " + parts : "")]);
  } else if (g.supported) {
    st.push(["info", "info", "Можно русифицировать", "Движок поддерживается: текст переведётся файлами, программа во время игры не нужна."]);
  } else {
    const why = (g.sources || []).includes("xbox") ? "Файлы игр Game Pass / Microsoft Store защищены." : "Этот движок пока не переводится файлами.";
    st.push(["info", "live", "Только живой перевод", why + " Играйте с оверлеем: перевод появится поверх игры."]);
  }
  if (g.live) st.push(["live", "live", `Допереведено на лету: ${g.live}`, "Столько строк игра собрала кодом — их перевела живая «доводка». Они войдут и в архив-русификатор."]);
  $("#gmState").innerHTML = st.map(([c, i, t, d]) => `<div class="state-box ${c}">${icon(i)}<div><b>${esc(t)}</b><span class="muted">${esc(d)}</span></div></div>`).join("");

  const acts = [];
  if (g.russified && g.intact === false) acts.push(["primary", "refresh", "Переустановить перевод", "reapply"]);
  else if (!g.russified && g.supported) acts.push(["primary", "globe", "Русифицировать", "russify"]);
  if (g.russified && g.intact !== false) acts.push(["primary", "play", "Играть", "play"]);
  else acts.push(["", "play", "Играть", "play"]);
  if (!g.supported) acts.push(["", "live", "Живой перевод", "live"]);
  if (g.russified && !g.from_package) acts.push(["", "package", "Создать файл русификатора", "export"]);
  if (g.russified) acts.push(["ghost", "undo", "Откатить", "restore"]);
  $("#gmActions").innerHTML = acts.map(([k, i, t, a]) => `<button class="btn ${k}" data-act="${a}">${icon(i)}${esc(t)}</button>`).join("");
  const links = [["folder-open", "Открыть папку", "folder"]];
  if (g.russified && g.engine === "unity") links.push(["link", "Ярлык на рабочий стол", "shortcut"]);
  if ((g.sources || []).includes("manual") || !(g.sources || []).length) links.push(["eye-off", "Убрать из списка", "hide"]);
  else links.push(["eye-off", "Скрыть", "hide"]);
  $("#gmLinks").innerHTML = links.map(([i, t, a]) => `<button class="btn ghost sm" data-act="${a}">${icon(i)}${esc(t)}</button>`).join("");
  $$("#gameModal [data-act]").forEach((b) => b.addEventListener("click", () => gameAction(b.dataset.act, g)));
  m.classList.remove("hidden");
}

async function gameAction(act, g) {
  const close = () => $("#gameModal").classList.add("hidden");
  if (act === "russify" || act === "reapply") {
    close();
    await detectGame(g.path);
    showPage("main");
    if (act === "reapply") { S.reuseNext = true; toast("Нажмите «Русифицировать» — готовые строки возьмутся из памяти"); }
  } else if (act === "play") {
    const r = await api().play_game(g.path, g.appid || "");
    if (!r.ok) toast(r.error, true); else toast("Запускаю «" + g.title + "»…");
  } else if (act === "export") {
    close();
    await detectGame(g.path, true);
    openExport();
  } else if (act === "restore") {
    restoreGame(g.path);
  } else if (act === "folder") {
    api().open_folder(g.path);
  } else if (act === "shortcut") {
    const r = await api().desktop_shortcut(g.path);
    toast(r.ok ? "Ярлык «" + r.name + "» создан на рабочем столе" : r.error, !r.ok);
  } else if (act === "live") {
    close();
    showPage("live");
  } else if (act === "hide") {
    await api().library_hide(g.path);
    S.lib.games = S.lib.games.filter((x) => x.path !== g.path);
    close();
    renderGames();
    toast("Игра убрана из списка");
  }
}

async function renderLibFolders() {
  const folders = await api().library_folders();
  $("#setLibFolders").innerHTML = folders.length ? folders.map((f) => `<div class="folder-item"><span class="mono small">${esc(f)}</span>
    <button class="icon-btn sm" data-rmf="${esc(f)}" title="Убрать">${icon("x")}</button></div>`).join("")
    : '<span class="muted small">Не добавлены.</span>';
  $$("[data-rmf]").forEach((b) => b.addEventListener("click", async () => { await api().library_remove_folder(b.dataset.rmf); renderLibFolders(); }));
}

/* ---------------- живой перевод ---------------- */

function startLivePolling() {
  refreshLive();
  clearInterval(S.liveTimer);
  S.liveTimer = setInterval(refreshLive, 1500);
}

function stopLivePolling() {
  clearInterval(S.liveTimer);
  S.liveTimer = null;
}

async function refreshLive() {
  const st = await api().overlay_status();
  S.liveSt = st;
  $("#navLiveDot").classList.toggle("hidden", !st.running);
  if (!$("#page-live").classList.contains("active")) return;
  const on = !!st.running;
  $("#liveHero").classList.toggle("on", on);
  let title = "Выключен", sub = "Включите один раз — дальше перевод сам появляется в играх, пока работает значок в трее.";
  if (!st.windows) { title = "Только для Windows"; sub = "Живой перевод использует распознавание текста Windows."; }
  else if (on && st.paused) { title = "Перевод скрыт"; sub = "Нажмите Alt+T в игре или «Показать перевод»."; }
  else if (on && st.ocr && st.ocr.starting) { title = "Запускается…"; sub = "Готовлю распознавание текста."; }
  else if (on && st.ocr && !st.ocr.ok && st.ocr.code) { title = "Не хватает распознавания текста"; sub = "Без него перевод не появится — см. ниже."; }
  else if (on && st.game) { title = "Переводит"; sub = "Перевод появляется поверх игры, как только текст перестаёт печататься."; }
  else if (on) { title = "Включён — ждёт игру"; sub = "Запустите игру: как только она на экране, перевод появится поверх неё."; }
  $("#liveTitle").textContent = title;
  $("#liveSub").textContent = sub;
  $("#liveCount").classList.toggle("hidden", !(on && st.count));
  $("#liveCount").textContent = `переведено фраз: ${st.count || 0}`;

  const g = on ? st.game : null;
  $("#liveNow").classList.toggle("hidden", !g);
  if (g) {
    $("#liveGame").textContent = "Сейчас: " + g.title;
    $("#liveReason").textContent = g.reason + (g.region ? " · выбрана область текста" : "");
    $("#livePauseTxt").textContent = st.paused ? "Показать перевод" : "Скрыть перевод";
    $("#liveRegionClear").classList.toggle("hidden", !g.region);
  }
  const ocr = st.ocr || {};
  const needOcr = on && !ocr.ok && !ocr.starting && ocr.code;
  $("#liveOcr").classList.toggle("hidden", !needOcr);
  if (needOcr) {
    const noLang = ocr.code === "no_language";
    $("#liveOcrTitle").textContent = noLang ? "Нужно распознавание английского текста" : "Распознавание текста не запустилось";
    $("#liveOcrText").textContent = noLang
      ? "Это бесплатный компонент Windows. Установка займёт минуту (нужны права администратора), потом нажмите «Проверить снова»."
      : (ocr.error || "Подробности — в журнале live.log в папке данных программы.");
    $("#liveOcrInstall").classList.toggle("hidden", !noLang);
  }
  if (on && st.translator && st.translator.error) {
    $("#liveSub").textContent = "Переводчик: " + st.translator.error;
  }
  renderLiveSettings();
  const feed = (st.recent || []);
  $("#liveFeed").innerHTML = feed.length ? feed.map((r) => `<div class="feed-item"><div class="src">${esc(r.src)}</div>${esc(r.tr)}</div>`).join("")
    : '<span class="muted small">Здесь появятся переведённые фразы.</span>';
}

function renderLiveSettings() {
  const st = S.liveSt;
  if (!st || !st.settings) return;
  const cfg = st.settings;
  const mode = cfg.live_mode || "auto";
  $$("#liveMode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#liveModeHint").textContent = {
    auto: "Тот же способ, что выбран на вкладке «Русификация». Если он недоступен — машинный.",
    machine: "Самый быстрый, офлайн и не занимает видеокарту — лучший выбор для живого перевода.",
    cloud: "Переводит лучше, но каждая новая фраза появляется через 1–3 секунды. Нужен ключ на вкладке «Русификация».",
    local: "Качественно и офлайн, но нейросеть делит видеокарту с игрой — на картах до 8 ГБ возможны подтормаживания.",
  }[mode] || "";
  const scale = cfg.live_font_scale || 1, op = cfg.live_opacity || 0.86;
  $("#liveScale").value = Math.round(scale * 100);
  $("#liveOpacity").value = Math.round(op * 100);
  $("#liveScaleVal").textContent = Math.round(scale * 100) + "%";
  $("#liveOpacityVal").textContent = Math.round(op * 100) + "%";
  $("#livePlate").style.fontSize = (15 * scale).toFixed(1) + "px";
  $("#livePlate").style.background = `rgba(14,16,24,${op})`;
  $("#liveAutostart").checked = !!st.autostart;
  const list = (items, key) => items.length ? items.map((p) => `<div class="folder-item"><span class="mono small">${esc(p)}</span>
    <button class="icon-btn sm" data-rm-live="${key}" data-path="${esc(p)}" title="Убрать">${icon("x")}</button></div>`).join("")
    : '<span class="muted small">Пусто.</span>';
  $("#liveAlways").innerHTML = list(cfg.live_always || [], "live_always");
  $("#liveNever").innerHTML = list(cfg.live_never || [], "live_never");
  $$("[data-rm-live]").forEach((b) => b.addEventListener("click", async () => {
    const key = b.dataset.rmLive;
    const next = (cfg[key] || []).filter((p) => p !== b.dataset.path);
    await api().overlay_settings({ [key]: next });
    cfg[key] = next;
    renderLiveSettings();
  }));
}

/* ---------------- установка архива ---------------- */

async function openImport(path) {
  const r = await api().import_open(path || "");
  if (!r.ok) { if (r.error) toast(r.error, true); return; }
  S.imp = { path: r.path, game: r.game };
  $("#imInfo").innerHTML = `<b>${esc(r.title)}</b> · ${esc(r.engine)}` + (r.total ? ` · переведено ${r.translated} из ${r.total} строк` : "") +
    (r.translator ? ` · ${esc(r.translator)}` : "") + (r.created ? `<br><span class="small">создан ${esc(r.created)}</span>` : "");
  $("#imGame").textContent = r.game || "Игра не найдена — укажите папку";
  setImportCheck(!!r.game, r.exact, r.game ? r.check : "Нажмите «Изменить…» и выберите папку игры (где её .exe).");
  $("#imProgress").classList.add("hidden");
  $("#imMsgs").innerHTML = "";
  $("#imGo").classList.remove("hidden");
  $("#importModal").classList.remove("hidden");
}

function setImportCheck(ok, exact, text) {
  const el = $("#imCheck");
  el.textContent = text || "";
  el.style.color = ok ? (exact ? "var(--ok)" : "var(--warn)") : "var(--danger)";
  $("#imGo").disabled = !ok || S.busy;
}

async function runImport() {
  if (!S.imp || !S.imp.game) return;
  const r = await api().import_install(S.imp.path, S.imp.game);
  if (!r.ok) return toast(r.error, true);
  S.busy = true;
  $("#imGo").disabled = true;
  const box = $("#imProgress");
  box.classList.remove("hidden");
  box.querySelector(".bar").classList.add("indeterminate");
  box.querySelector(".dl-text").textContent = "Установка…";
}

function onImportProgress(ev) {
  const box = $("#imProgress");
  const bar = box.querySelector(".bar");
  if (ev.fraction != null) { bar.classList.remove("indeterminate"); bar.querySelector(".bar-fill").style.width = (ev.fraction * 100).toFixed(1) + "%"; }
  box.querySelector(".dl-text").textContent = ev.message || "";
}

async function onImportDone(ev) {
  S.busy = false;
  $("#imProgress").classList.add("hidden");
  updateRunState();
  if (!ev.ok) {
    $("#imGo").disabled = false;
    $("#imMsgs").innerHTML = `<div class="msg e">${icon("x-circle")}<span>${esc(ev.error)}</span></div>`;
    return;
  }
  $("#imGo").classList.add("hidden");
  $("#imMsgs").innerHTML = [`<div class="msg n">${icon("check-circle")}<span>Готово! Русификатор установлен — запускайте игру.</span></div>`]
    .concat((ev.notes || []).map((m) => `<div class="msg w">${icon("alert")}<span>${esc(m)}</span></div>`)).join("");
  toast("Русификатор установлен");
  if (S.lib.loaded) { const g = await api().game_status(ev.game); if (g && g.path) { upsertGame(g); renderGames(); } }
  if (S.game && S.game.path === ev.game) detectGame(ev.game, true);
}

/* ---------------- архив-русификатор ---------------- */

async function openExport() {
  if (!S.game) return;
  if (!S.game.can_export) {
    await detectGame(S.game.path, true);
    if (!S.game || !S.game.can_export) return toast((S.game && S.game.export_reason) || "Сначала русифицируйте игру.", true);
  }
  const d = await api().export_defaults();
  $("#exportGame").textContent = "«" + S.game.name + "»";
  $("#exportDir").textContent = d.dir;
  $("#exportCredit").checked = d.credit;
  $$(".tgName").forEach((el) => { el.textContent = S.init.order.telegram; });
  $("#exportProgress").classList.add("hidden");
  $("#exportDone").classList.add("hidden");
  $("#exportMsgs").innerHTML = "";
  $("#exportGo").disabled = false;
  $("#exportGo").classList.remove("hidden");
  $("#exportModal").classList.remove("hidden");
}

async function runExport() {
  const r = await api().export_package(S.game.path, $("#exportDir").textContent, $("#exportCredit").checked);
  if (!r.ok) return toast(r.error, true);
  S.busy = true;
  updateRunState();
  $("#exportGo").disabled = true;
  $("#exportDone").classList.add("hidden");
  $("#exportMsgs").innerHTML = "";
  const box = $("#exportProgress");
  box.classList.remove("hidden");
  box.querySelector(".bar").classList.add("indeterminate");
  box.querySelector(".dl-text").textContent = "Подготовка…";
}

function onExportProgress(ev) {
  const box = $("#exportProgress");
  const bar = box.querySelector(".bar");
  if (ev.fraction != null) {
    bar.classList.remove("indeterminate");
    bar.querySelector(".bar-fill").style.width = (ev.fraction * 100).toFixed(1) + "%";
  } else bar.classList.add("indeterminate");
  box.querySelector(".dl-text").textContent = ev.message || "";
}

function onExportDone(ev) {
  S.busy = false;
  updateRunState();
  $("#exportProgress").classList.add("hidden");
  $("#exportGo").disabled = false;
  if (!ev.ok) {
    $("#exportMsgs").innerHTML = `<div class="msg e">${icon("x-circle")}<span>${esc(ev.error)}</span></div>`;
    return;
  }
  S.exportPath = ev.path;
  $("#exportName").textContent = ev.name;
  $("#exportMeta").textContent = `${size(ev.size)} · сохранён в ${ev.path.replace(/[\\/][^\\/]*$/, "")}`;
  $("#exportDone").classList.remove("hidden");
  $("#exportGo").classList.add("hidden");
  $("#exportMsgs").innerHTML = (ev.notes || []).map((m) => `<div class="msg w">${icon("alert")}<span>${esc(m)}</span></div>`).join("");
  toast("Архив-русификатор готов");
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
