const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let account = null;
let secret = "";
let taskDetailPrompts = {original: "", processed: ""};
let docsLoaded = false;
let ledgerLimit = 20;
let activeApiTab = "keys";
let activeOverviewRecord = localStorage.getItem("equaiOverviewRecord") === "ledger" ? "ledger" : "calls";
let studioKind = "text";
let studioPollTimer = 0;
let studioEventSource = null;
const STUDIO_ACTIVE_REFRESH_MS = 3000;
let studioSubmission = null;
let studioTaskItems = [];
let studioSelectedTaskId = localStorage.getItem("equaiStudioTaskId") || "";
let studioSelectedTaskDetail = null;
let balanceHasAnimated = false;
let ambientLoginTimer = 0;
let sessionExpiredHandled = false;
const studioReferences = {images: [], videos: [], audios: []};

function loadAmbientVideo(video) {
  if (!video || video.dataset.loaded === "true") return;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  const constrained = reduced || matchMedia("(max-width: 700px)").matches || connection?.saveData || (navigator.deviceMemory && navigator.deviceMemory < 4);
  if (constrained) return;
  const webm = document.createElement("source");
  webm.src = video.dataset.webm;
  webm.type = "video/webm";
  const mp4 = document.createElement("source");
  mp4.src = video.dataset.mp4;
  mp4.type = "video/mp4";
  video.append(webm, mp4);
  video.dataset.loaded = "true";
  video.load();
  video.play().catch(() => null);
}

function syncAmbientVideo() {
  const login = $("#ambientVideo"), consoleVideo = $("#consoleAmbientVideo");
  if (document.body.classList.contains("auth-mode")) {
    login?.pause(); consoleVideo?.pause();
    return;
  }
  const active = document.body.classList.contains("auth-mode") ? login : consoleVideo;
  const inactive = active === login ? consoleVideo : login;
  clearTimeout(ambientLoginTimer);
  if (inactive) inactive.pause();
  if (active?.dataset.loaded !== "true") {
    ambientLoginTimer = setTimeout(() => {
      if (!document.body.classList.contains("auth-mode")) loadAmbientVideo(active);
    }, 1800);
  } else {
    loadAmbientVideo(active);
    active?.play().catch(() => null);
  }
}

syncAmbientVideo();

function showLoginForm(mode = "login") {
  if (mode !== "login" && mode !== "register") return;
  $("#loginCoverMusic")?.pause();
  $("#bgm-control")?.classList.remove("is-playing");
  $("#bgm-control")?.setAttribute("aria-pressed", "false");
  $("#authView").classList.remove("hidden");
  document.body.classList.remove("landing-active");
  document.body.classList.add("auth-form-active");
  const tab = $(`[data-auth="${mode}"]`);
  if (tab && !tab.classList.contains("active")) tab.click();
  requestAnimationFrame(() => $(mode === "login" ? "#loginUsername" : "#regUsername")?.focus({preventScroll:true}));
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-open-auth]");
  if (button) showLoginForm(button.dataset.openAuth);
});
const aboutDialog = $("#aboutDialog");
$("#aboutOpen")?.addEventListener("click", () => aboutDialog?.showModal());
$("#aboutClose")?.addEventListener("click", () => aboutDialog?.close());
aboutDialog?.addEventListener("click", event => {
  if (event.target === aboutDialog) aboutDialog.close();
});
$$('[data-return-cover]').forEach(button => button.addEventListener("click", () => {
  $("#authView").classList.add("hidden");
  document.body.classList.remove("auth-form-active");
  document.body.classList.add("landing-active");
}));

function applyTheme(theme, persist = true) {
  const value = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = value;
  if (persist) {
    try { localStorage.setItem("equaiTheme", value); } catch {}
  }
  const button = $("#themeToggle");
  if (!button) return;
  const light = value === "light";
  button.innerHTML = light
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 15.1A8.4 8.4 0 0 1 8.9 3.8 8.5 8.5 0 1 0 20.2 15.1Z"/></svg><span>暗色</span>'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg><span>浅色</span>';
  button.setAttribute("aria-label", light ? "切换到暗色模式" : "切换到浅色模式");
  button.setAttribute("aria-pressed", String(light));
  button.title = light ? "切换到暗色模式" : "切换到浅色模式";
}
applyTheme(document.documentElement.dataset.theme, false);
$("#themeToggle")?.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}
function stamp(value) { return value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12:false}) : "—"; }
function duration(value) {
  if (value == null) return "—";
  const m = Math.floor(value / 60), s = Math.round(value % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}
function statusLabel(status) {
  return ({pending:"待开通",active:"正常",disabled:"已停用",suspended_failures:"连续失败暂停",queued:"排队",dispatching:"投递中",running:"运行中",completed:"成功",failed:"失败",cancelled:"已取消",suspended_balance:"余额不足",suspended_account:"账户暂停",revoked:"已撤销"})[status] || status;
}
function toast(message) {
  const el = $("#toast"); el.textContent = message; el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 2600);
}
async function api(path, options = {}) {
  const response = await fetch(path, {credentials:"same-origin", ...options, headers:{...(options.body && !(options.body instanceof FormData) ? {"Content-Type":"application/json"} : {}), ...(options.headers || {})}});
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) handleSessionExpired();
  if (!response.ok) {
    const error = new Error(body.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function uploadForm(path, form, headers = {}, onPhase = () => {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.withCredentials = true;
    xhr.timeout = 10 * 60 * 1000;
    Object.entries(headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));
    xhr.upload.addEventListener("progress", event => {
      if (!event.lengthComputable) {
        onPhase({phase:"uploading", progress:null});
        return;
      }
      onPhase({phase:"uploading", progress:Math.min(100, Math.round(event.loaded / event.total * 100))});
    });
    xhr.upload.addEventListener("load", () => onPhase({phase:"creating", progress:100}));
    xhr.addEventListener("load", () => {
      const body = (() => { try { return JSON.parse(xhr.responseText || "{}"); } catch { return {}; } })();
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        if (xhr.status === 401) handleSessionExpired();
        const error = new Error(body.detail || `请求失败 (${xhr.status})`);
        error.status = xhr.status;
        reject(error);
      }
    });
    xhr.addEventListener("error", () => reject(new Error("上传连接中断，请检查网络后重试")));
    xhr.addEventListener("timeout", () => reject(new Error("上传超过 10 分钟，已停止等待，请检查网络后重试")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(form);
  });
}

function putDirectChunk(url, body, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.timeout = 20 * 60 * 1000;
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.addEventListener("progress", event => {
      if (event.lengthComputable) onProgress(event.loaded);
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`素材直传失败 (${xhr.status})`));
    });
    xhr.addEventListener("error", () => reject(new Error("素材直传连接中断，请检查网络后重试")));
    xhr.addEventListener("timeout", () => reject(new Error("素材直传超时，请重新提交")));
    xhr.send(body);
  });
}

function mobileContentType(field, file) {
  const ext = file.name.toLowerCase().match(/\.[^.]+$/)?.[0] || "";
  const images = {".heic":"image/heic", ".heif":"image/heif", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp", ".gif":"image/gif", ".bmp":"image/bmp", ".tif":"image/tiff", ".tiff":"image/tiff"};
  const videos = {".mp4":"video/mp4", ".m4v":"video/x-m4v", ".mov":"video/quicktime", ".qt":"video/quicktime", ".webm":"video/webm", ".3gp":"video/3gpp", ".3g2":"video/3gpp2", ".mkv":"video/x-matroska", ".avi":"video/x-msvideo"};
  if (["images", "first_frame", "last_frame"].includes(field)) return images[ext] || (file.type.startsWith("image/") ? file.type : "image/jpeg");
  if (field === "videos") return videos[ext] || (file.type.startsWith("video/") ? file.type : "video/mp4");
  if (field === "audios") return file.type.startsWith("audio/") ? file.type : "audio/mpeg";
  return file.type || "application/octet-stream";
}

async function putDirectAsset(grant, file, onProgress) {
  const chunkBytes = Number(grant.chunk_bytes || 0);
  const parallelism = Math.max(1, Math.min(6, Number(grant.parallelism || 1)));
  async function retryUpload(url, body, progress, fallbackUrl = "") {
    let last;
    for (let attempt = 0; attempt < 3; attempt++) {
      try { return await putDirectChunk(url, body, grant.content_type, progress); }
      catch (error) { last = error; if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 700 * 2 ** attempt)); }
    }
    // If the direct Tencent origin is unreachable from a particular mobile
    // carrier, transparently retry the same signed chunk through the public
    // origin instead of making the user choose a different upload mode.
    if (fallbackUrl && fallbackUrl !== url) {
      for (let attempt = 0; attempt < 2; attempt++) {
        try { return await putDirectChunk(fallbackUrl, body, grant.content_type, progress); }
        catch (error) { last = error; if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 700)); }
      }
    }
    throw last;
  }
  if (!chunkBytes || file.size <= chunkBytes) {
    return retryUpload(grant.upload_url, file, onProgress, grant.fallback_upload_url || "");
  }
  const count = Math.ceil(file.size / chunkBytes), loaded = new Array(count).fill(0);
  let cursor = 0;
  async function worker() {
    while (cursor < count) {
      const index = cursor++;
      const start = index * chunkBytes, end = Math.min(file.size, start + chunkBytes);
      const separator = grant.upload_url.includes("?") ? "&" : "?";
      const url = `${grant.upload_url}${separator}part=${index}&parts=${count}`;
      const fallbackBase = grant.fallback_upload_url || "";
      const fallbackSeparator = fallbackBase.includes("?") ? "&" : "?";
      const fallbackUrl = fallbackBase ? `${fallbackBase}${fallbackSeparator}part=${index}&parts=${count}` : "";
      await retryUpload(url, file.slice(start, end, grant.content_type), value => {
        loaded[index] = value;
        onProgress(loaded.reduce((sum, size) => sum + size, 0));
      }, fallbackUrl);
      loaded[index] = end - start;
      onProgress(loaded.reduce((sum, size) => sum + size, 0));
    }
  }
  await Promise.all(Array.from({length:Math.min(parallelism, count)}, worker));
}

async function prepareStudioDirectUpload(form, onProgress = () => {}) {
  const files = [...form.entries()]
    .filter(([, value]) => value instanceof File)
    .map(([field, file]) => ({field, file}));
  if (!files.length) return form;
  const granted = await api("/api/account/uploads/presign", {
    method:"POST",
    body:JSON.stringify({
      kind:String(form.get("kind") || ""),
      files:files.map(({field, file}) => ({
        field,
        filename:file.name,
        content_type:mobileContentType(field, file),
        size_bytes:file.size,
      })),
    }),
  });
  if (granted.enabled === false) return form;
  if (!Array.isArray(granted.items) || granted.items.length !== files.length) {
    throw new Error("素材上传授权数量不正确");
  }
  const loaded = new Array(files.length).fill(0);
  const total = files.reduce((sum, item) => sum + item.file.size, 0);
  let cursor = 0;
  async function worker() {
    while (cursor < files.length) {
      const index = cursor++;
      await putDirectAsset(granted.items[index], files[index].file, value => {
        loaded[index] = value;
        onProgress(Math.min(100, Math.round(loaded.reduce((sum, n) => sum + n, 0) / total * 100)));
      });
      loaded[index] = files[index].file.size;
      onProgress(Math.min(100, Math.round(loaded.reduce((sum, n) => sum + n, 0) / total * 100)));
    }
  }
  await Promise.all(Array.from({length:Math.min(3, files.length)}, worker));
  const directForm = new FormData();
  for (const [key, value] of form.entries()) {
    if (!(value instanceof File)) directForm.append(key, value);
  }
  directForm.append("direct_assets", JSON.stringify(granted.items.map(({upload_url, ...item}) => item)));
  return directForm;
}
function showSecret(title, value, notice) {
  secret = value;
  $("#secretTitle").textContent = title;
  $("#secretValue").textContent = value;
  $("#secretNotice").textContent = notice;
  $("#secretModal").classList.remove("hidden");
}

$$('[data-auth]').forEach(btn => btn.addEventListener("click", () => {
  $$('[data-auth]').forEach(x => x.classList.toggle("active", x === btn));
  $("#loginForm").classList.toggle("hidden", btn.dataset.auth !== "login");
  $("#registerForm").classList.toggle("hidden", btn.dataset.auth !== "register");
}));

$("#loginForm").addEventListener("input", () => {
  $("#loginError").textContent = "";
});

$$('[data-toggle-password]').forEach(button => button.addEventListener("click", () => {
  const input = document.getElementById(button.dataset.togglePassword);
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  button.classList.toggle("showing", !showing);
  button.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
}));

const authShell = $("#authView");
if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
  let frame = 0;
  authShell.addEventListener("pointermove", event => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      authShell.style.setProperty("--pointer-x", `${(event.clientX / innerWidth - .5) * 2}`);
      authShell.style.setProperty("--pointer-y", `${(event.clientY / innerHeight - .5) * 2}`);
    });
  });
}

$("#loginForm").addEventListener("submit", async event => {
  event.preventDefault(); $("#loginError").textContent = "";
  try {
    await api("/api/account/login", {method:"POST", body:JSON.stringify({username:$("#loginUsername").value, password:$("#loginPassword").value})});
    await enterConsole();
  } catch (error) { $("#loginError").textContent = error.message; }
});
$("#registerForm").addEventListener("submit", async event => {
  event.preventDefault(); $("#registerError").textContent = "";
  try {
    const result = await api("/api/account/register", {method:"POST", body:JSON.stringify({username:$("#regUsername").value, password:$("#regPassword").value})});
    showSecret("保存账户恢复码", result.recovery_code, `商户 ID：${result.merchant_id}。恢复码只显示这一次，请离线保存。`);
    await enterConsole();
  } catch (error) { $("#registerError").textContent = error.message; }
});
$("#logoutBtn").addEventListener("click", async () => {
  await api("/api/account/logout", {method:"POST"}).catch(() => null);
  clearTimeout(studioPollTimer);
  account = null; document.body.classList.add("auth-mode", "landing-active"); document.body.classList.remove("auth-form-active"); $("#consoleView").classList.add("hidden"); $("#authView").classList.add("hidden"); $("#loginLanding").classList.remove("hidden"); $("#logoutBtn").classList.add("hidden"); syncAmbientVideo();
});
$("#copySecret").addEventListener("click", async () => { await navigator.clipboard.writeText(secret); toast("已复制"); });
$("#closeSecret").addEventListener("click", () => $("#secretModal").classList.add("hidden"));
$$('.open-community').forEach(button => button.addEventListener("click", () => $("#communityModal").classList.remove("hidden")));
$("#closeCommunity").addEventListener("click", () => $("#communityModal").classList.add("hidden"));
$("#communityModal").addEventListener("click", event => {
  if (event.target === event.currentTarget) event.currentTarget.classList.add("hidden");
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") $("#communityModal").classList.add("hidden");
});

const APP_PAGES = new Set(["studio", "commerce-remix", "digital-human", "editor", "image-edit"]);

function switchApiTab(name) {
  if (name !== "keys" && name !== "docs") return;
  activeApiTab = name;
  $$('[data-api-tab]').forEach(button => {
    const active = button.dataset.apiTab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#apiSectionKeys").classList.toggle("hidden", name !== "keys");
  $("#apiSectionDocs").classList.toggle("hidden", name !== "docs");
  if (name === "keys") loadKeys();
  if (name === "docs") loadDocs();
}

function setOverviewRecord(name, persist = true) {
  activeOverviewRecord = name === "ledger" ? "ledger" : "calls";
  const showLedger = activeOverviewRecord === "ledger";
  $("#pointsLedger").classList.toggle("hidden", !showLedger);
  $("#callHistory").classList.toggle("hidden", showLedger);
  $$('[data-overview-record]').forEach(button => {
    const selected = button.dataset.overviewRecord === activeOverviewRecord;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (persist) {
    try { localStorage.setItem("equaiOverviewRecord", activeOverviewRecord); } catch {}
  }
}
setOverviewRecord(activeOverviewRecord, false);

function switchPage(name) {
  if (name === "remix" || name === "remix2") name = "apps";
  if (name === "calls" || name === "ledger") name = "overview";
  if (name === "keys") { activeApiTab = "keys"; name = "api"; }
  if (name === "docs") { activeApiTab = "docs"; name = "api"; }
  if (name !== "studio") clearTimeout(studioPollTimer);
  $$(".page").forEach(page => page.classList.toggle("hidden", page.id !== `page-${name}`));
  const navPage = APP_PAGES.has(name) ? "apps" : name;
  $$("[data-page]").forEach(btn => btn.classList.toggle("active", btn.dataset.page === navPage));
  location.hash = name;
  if (name === "overview") { loadCalls(); loadLedger(); }
  if (name === "api") switchApiTab(activeApiTab);
  if (name === "studio") resumeStudioTask();
  document.dispatchEvent(new CustomEvent("creative-page", {detail: name}));
}
$$('[data-page]').forEach(btn => btn.addEventListener("click", () => switchPage(btn.dataset.page)));
$$('[data-api-tab]').forEach(btn => btn.addEventListener("click", () => switchApiTab(btn.dataset.apiTab)));
$$('[data-overview-record]').forEach(btn => btn.addEventListener("click", () => setOverviewRecord(btn.dataset.overviewRecord)));
$$('[data-open-app]').forEach(btn => btn.addEventListener("click", () => switchPage(btn.dataset.openApp)));

// Application discovery is isolated to the tool directory; existing tool pages keep their own flows.
const appFavoritesKey = "equaiAppFavorites";
let appCategory = "全部";
let showAppFavorites = false;
function readAppFavorites() {
  try { return new Set(JSON.parse(localStorage.getItem(appFavoritesKey) || "[]")); }
  catch { return new Set(); }
}
function applyAppDirectoryFilter() {
  const favorites = readAppFavorites();
  let visible = 0;
  $$("#appLaunchGrid .app-launch-card").forEach(card => {
    const matchesCategory = appCategory === "全部" || card.dataset.appCategory === appCategory;
    const matchesFavorites = !showAppFavorites || favorites.has(card.dataset.appId);
    const shown = matchesCategory && matchesFavorites;
    card.classList.toggle("is-filtered-out", !shown);
    visible += Number(shown);
    const favorite = card.querySelector(".app-favorite");
    const active = favorites.has(card.dataset.appId);
    favorite?.setAttribute("aria-pressed", String(active));
    if (favorite) favorite.textContent = active ? "♥" : "♡";
  });
  const empty = $("#appsEmptyState");
  empty?.classList.toggle("hidden", visible > 0);
  const count = $("#page-apps .apps-count");
  if (count) count.textContent = `${visible} 个工具`;
}
function toggleAppFavorite(card) {
  if (!card) return;
  const favorites = readAppFavorites();
  const appId = card.dataset.appId;
  favorites.has(appId) ? favorites.delete(appId) : favorites.add(appId);
  try { localStorage.setItem(appFavoritesKey, JSON.stringify([...favorites])); } catch {}
  applyAppDirectoryFilter();
}
document.addEventListener("click", event => {
  const favorite = event.target.closest(".app-favorite");
  if (favorite) {
    event.preventDefault(); event.stopPropagation();
    toggleAppFavorite(favorite.closest(".app-launch-card"));
  }
}, true);
document.addEventListener("keydown", event => {
  const favorite = event.target.closest?.(".app-favorite");
  if (favorite && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault(); event.stopPropagation();
    toggleAppFavorite(favorite.closest(".app-launch-card"));
  }
});
$$('#page-apps [role="tab"][data-app-category]').forEach(button => button.addEventListener("click", () => {
  appCategory = button.dataset.appCategory;
  showAppFavorites = false;
  $("#appFavoritesToggle")?.setAttribute("aria-pressed", "false");
  $$('[data-app-category][role="tab"]').forEach(tab => tab.setAttribute("aria-selected", String(tab === button)));
  applyAppDirectoryFilter();
}));
$("#appFavoritesToggle")?.addEventListener("click", event => {
  showAppFavorites = !showAppFavorites;
  event.currentTarget.setAttribute("aria-pressed", String(showAppFavorites));
  if (showAppFavorites) $$('[data-app-category][role="tab"]').forEach(tab => tab.setAttribute("aria-selected", "false"));
  else {
    appCategory = "全部";
    $('[data-app-category="全部"]')?.setAttribute("aria-selected", "true");
  }
  applyAppDirectoryFilter();
});
$$('[data-app-example]').forEach(button => button.addEventListener("click", () => {
  const input = $("#appRecommendQuery");
  if (!input) return;
  input.value = button.dataset.appExample || "";
  input.focus();
  input.form?.requestSubmit();
}));
$("#appRecommendForm")?.addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#appRecommendQuery"), submit = $("#appRecommendSubmit");
  const status = $("#appRecommendStatus"), results = $("#appRecommendResults");
  const query = input?.value.trim() || "";
  if (query.length < 2) {
    status.textContent = "多说一点你想完成的事情，我来帮你找。";
    status.classList.add("is-error");
    input?.focus();
    return;
  }
  submit.disabled = true;
  status.classList.remove("is-error");
  status.textContent = "正在为你找合适的工具…";
  results.classList.add("hidden");
  results.replaceChildren();
  try {
    const data = await api("/api/account/apps/recommend", {method:"POST", body:JSON.stringify({query})});
    const available = new Map($$("#appLaunchGrid .app-launch-card").map(card => [card.dataset.appId, card]));
    const matches = (data.results || []).filter(item => available.has(item.app_id));
    if (!matches.length) throw new Error("暂时没找到合适的应用，请换种说法或使用分类浏览");
    results.innerHTML = matches.map(item => `<button type="button" class="app-recommend-result" data-recommended-app="${esc(item.app_id)}"><small>${esc(item.category)} · 为你推荐</small><strong>${esc(item.name)} →</strong><span>${esc(item.reason)}</span></button>`).join("");
    results.classList.remove("hidden");
    status.textContent = "找到这些工具，点一下就能开始。";
  } catch (error) {
    const unavailable = /请求失败 \((?:404|405)\)/.test(error.message || "")
      || error.message === "Method Not Allowed" || error.message === "Not Found";
    status.textContent = window.__EQUAI_LOCAL_PREVIEW__
      && (error instanceof TypeError || /请求失败 \(\d{3}\)/.test(error.message))
      ? "当前是本地静态预览，未连接 API 后端；登录后会由 DeepSeek 帮你推荐应用。"
      : unavailable
        ? "AI 推荐暂时不可用，请使用下方分类和工具卡继续操作。"
      : (error.message || "AI 推荐暂时不可用，请使用下方分类浏览应用。");
    status.classList.add("is-error");
  } finally {
    submit.disabled = false;
  }
});
$("#appRecommendResults")?.addEventListener("click", event => {
  const result = event.target.closest("[data-recommended-app]");
  if (!result) return;
  const appId = result.dataset.recommendedApp;
  const query = $("#appRecommendQuery")?.value.trim() || "";
  switchPage(appId);
  const promptByApp = {studio:"#studioPrompt", editor:"#editorPrompt", "image-edit":"#editPrompt", music:"#musicPrompt"};
  const prompt = promptByApp[appId] ? $(promptByApp[appId]) : null;
  if (prompt && query && !prompt.value.trim()) {
    prompt.value = query;
    prompt.dispatchEvent(new Event("input", {bubbles:true}));
  }
});
applyAppDirectoryFilter();
$$('[data-go]').forEach(btn => btn.addEventListener("click", () => {
  if (btn.dataset.scrollTarget === "callHistory") setOverviewRecord("calls");
  if (btn.dataset.scrollTarget === "pointsLedger") setOverviewRecord("ledger");
  switchPage(btn.dataset.go);
  if (btn.dataset.scrollTarget) {
    requestAnimationFrame(() => document.getElementById(btn.dataset.scrollTarget)?.scrollIntoView({behavior:"smooth", block:"start"}));
  }
}));

function renderAccount(current) {
  sessionExpiredHandled = false;
  account = current;
  $("#merchantName").textContent = account.username; $("#merchantId").textContent = account.id;
  const balanceTarget = Number(account.balance);
  if (!balanceHasAnimated && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    balanceHasAnimated = true;
    const started = performance.now();
    const tick = now => {
      const k = Math.min(1, (now - started) / 1200);
      $("#balance").textContent = (balanceTarget * (1 - Math.pow(1 - k, 3))).toFixed(3);
      if (k < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  } else {
    $("#balance").textContent = balanceTarget.toFixed(3);
  }
  $("#reserved").textContent = Number(account.reserved).toFixed(3);
  $("#studioBalance").textContent = Number(account.balance).toFixed(3);
  $("#balanceRail").style.width = `${Math.min(100, Math.max(2, Number(account.balance) * 10))}%`;
  $("#completedCount").textContent = account.usage.completed; $("#activeCount").textContent = account.usage.active;
  const status = $("#accountStatus");
  if (status) { status.textContent = statusLabel(account.status); status.className = `status ${account.status}`; }
  $("#pendingNotice").classList.toggle("hidden", account.status !== "pending");
}

function handleSessionExpired() {
  if (!account || sessionExpiredHandled) return;
  sessionExpiredHandled = true;
  const currentAccount = account;
  fetch("/api/account/me", {credentials:"same-origin", cache:"no-store"}).then(response => {
    if (response.status !== 401 || account !== currentAccount) {
      sessionExpiredHandled = false;
      return;
    }
    account = null;
    clearTimeout(studioPollTimer);
    document.body.classList.add("auth-mode", "auth-form-active");
    document.body.classList.remove("landing-active");
    $("#consoleView").classList.add("hidden");
    $("#loginLanding").classList.add("hidden");
    $("#authView").classList.remove("hidden");
    const loginTab = $('[data-auth="login"]');
    if (loginTab && !loginTab.classList.contains("active")) loginTab.click();
    $("#loginError").textContent = "登录已过期，请重新登录后继续使用。";
    syncAmbientVideo();
  }).catch(() => { sessionExpiredHandled = false; });
}

async function refreshAccount() {
  const current = await api("/api/account/me");
  renderAccount(current);
  return current;
}

async function enterConsole(current = null) {
  current = current || await api("/api/account/me");
  document.body.classList.remove("auth-mode", "landing-active", "auth-form-active");
  $("#loginLanding").classList.add("hidden"); $("#authView").classList.add("hidden"); $("#consoleView").classList.remove("hidden"); $("#logoutBtn").classList.remove("hidden");
  syncAmbientVideo();
  renderAccount(current);
  $$(".api-origin").forEach(el => el.textContent = location.origin);
  switchPage(location.hash.slice(1) || "overview");
}


const STUDIO_KIND_LABEL = {text:"文生视频", image:"首帧 / 首尾帧", reference:"多参考"};
const STUDIO_REFERENCE_LABEL = {images:"图片", videos:"视频", audios:"音频"};
const STUDIO_REFERENCE_LIMIT = {images:9, videos:3, audios:3};

function studioQuality() {
  return $('[name="app_quality"]:checked')?.value || "720p";
}

function updateStudioPrice() {
  const quality = studioQuality();
  const price = {"720p": "1", "1080p": "2", "2k": "3"}[quality] || "1";
  $("#studioSubmitPrice").textContent = `将冻结 ${price} pt`;
}

function setStudioKind(kind) {
  studioKind = kind;
  $$('[data-studio-kind]').forEach(button => button.classList.toggle("active", button.dataset.studioKind === kind));
  $("#studio-image-assets").classList.toggle("hidden", kind !== "image");
  $("#studio-reference-assets").classList.toggle("hidden", kind !== "reference");
  $("#studioPromptHint").textContent = kind === "reference"
    ? "可用图一、图二或文件名描述素材关系；重名请用编号"
    : kind === "image" ? "可按图片名描述动作；首尾帧按上传位置衔接" : "描述画面、动作、镜头和声音";
}

function referenceCount() {
  return Object.values(studioReferences).reduce((count, items) => count + items.length, 0);
}

function renderStudioReferences() {
  const host = $("#studioReferenceList");
  const rows = [];
  for (const type of ["images", "videos", "audios"]) {
    studioReferences[type].forEach((item, index) => rows.push(`
      <div class="reference-file">
        <span class="reference-file-kind">
          ${type === "images" ? `<img src="${esc(item.previewUrl)}" alt="${esc(STUDIO_REFERENCE_LABEL[type])} ${index + 1} 预览">` : `<i>${type === "videos" ? "VID" : "AUD"}</i>`}
          <b>${esc(STUDIO_REFERENCE_LABEL[type])} ${index + 1}</b>
        </span>
        <small title="${esc(item.file.name)}">${esc(item.file.name)}${type === "videos" && item.useAudio ? " · 使用原声" : ""}</small>
        <span class="reference-file-actions">
          ${type === "videos" ? `<button type="button" data-ref-audio="${index}" aria-label="${item.useAudio ? "关闭" : "使用"}视频原声">♫</button>` : ""}
          <button type="button" data-ref-move="-1" data-ref-type="${type}" data-ref-index="${index}" aria-label="前移">↑</button>
          <button type="button" data-ref-move="1" data-ref-type="${type}" data-ref-index="${index}" aria-label="后移">↓</button>
          <button type="button" data-ref-remove data-ref-type="${type}" data-ref-index="${index}" aria-label="删除">×</button>
        </span>
      </div>`));
  }
  host.innerHTML = rows.join("") || '<div class="empty-reference">还没有添加参考素材</div>';
  host.querySelectorAll("[data-ref-remove]").forEach(button => button.addEventListener("click", () => {
    const [removed] = studioReferences[button.dataset.refType].splice(Number(button.dataset.refIndex), 1);
    if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
    renderStudioReferences();
  }));
  host.querySelectorAll("[data-ref-move]").forEach(button => button.addEventListener("click", () => {
    const list = studioReferences[button.dataset.refType];
    const from = Number(button.dataset.refIndex), to = from + Number(button.dataset.refMove);
    if (to < 0 || to >= list.length) return;
    [list[from], list[to]] = [list[to], list[from]];
    renderStudioReferences();
  }));
  host.querySelectorAll("[data-ref-audio]").forEach(button => button.addEventListener("click", () => {
    const item = studioReferences.videos[Number(button.dataset.refAudio)];
    if (item) item.useAudio = !item.useAudio;
    renderStudioReferences();
  }));
}

function addStudioReferences(type, files) {
  const list = studioReferences[type];
  const availableForType = STUDIO_REFERENCE_LIMIT[type] - list.length;
  const availableTotal = 12 - referenceCount();
  const accepted = [...files].slice(0, Math.max(0, Math.min(availableForType, availableTotal)));
  accepted.forEach(file => list.push({
    file,
    useAudio:type === "videos",
    previewUrl:type === "images" ? URL.createObjectURL(file) : "",
  }));
  if (accepted.length < files.length) toast(`参考${STUDIO_REFERENCE_LABEL[type]}或全部素材已达到数量上限`);
  renderStudioReferences();
}

$$('[data-studio-kind]').forEach(button => button.addEventListener("click", () => setStudioKind(button.dataset.studioKind)));
$$('[name="app_quality"]').forEach(input => input.addEventListener("change", updateStudioPrice));
function updateKeyframePreview(input, preview, name, fallback) {
  const previousUrl = preview.dataset.objectUrl;
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  const file = input.files[0];
  const slot = input.closest(".keyframe-slot");
  if (!file) {
    preview.removeAttribute("src");
    preview.removeAttribute("data-object-url");
    preview.classList.add("hidden");
    slot.classList.remove("has-preview", "portrait-preview", "landscape-preview");
    name.textContent = fallback;
    return;
  }
  const objectUrl = URL.createObjectURL(file);
  preview.dataset.objectUrl = objectUrl;
  preview.src = objectUrl;
  preview.classList.remove("hidden");
  slot.classList.add("has-preview");
  name.textContent = file.name;
  preview.onload = () => {
    const portrait = preview.naturalHeight > preview.naturalWidth;
    slot.classList.toggle("portrait-preview", portrait);
    slot.classList.toggle("landscape-preview", !portrait);
  };
}

$("#studioFirstFrame").addEventListener("change", event => {
  updateKeyframePreview(event.target, $("#studioFirstPreview"), $("#studioFirstName"), "决定视频从哪里开始");
});
$("#studioLastFrame").addEventListener("change", event => {
  updateKeyframePreview(event.target, $("#studioLastPreview"), $("#studioLastName"), "可选，决定视频在哪里结束");
});
for (const [selector, type] of [["#studioRefImages", "images"], ["#studioRefVideos", "videos"], ["#studioRefAudios", "audios"]]) {
  $(selector).addEventListener("change", event => {
    addStudioReferences(type, event.target.files);
    event.target.value = "";
  });
}

function studioFingerprint(formData) {
  const parts = [];
  for (const [key, value] of formData.entries()) {
    parts.push(value instanceof File ? `${key}:${value.name}:${value.size}:${value.lastModified}` : `${key}:${value}`);
  }
  return parts.join("|");
}

function buildStudioFormData() {
  const prompt = $("#studioPrompt").value.trim();
  if (!prompt) throw new Error("请填写画面描述");
  const form = new FormData();
  form.append("kind", studioKind);
  form.append("prompt", prompt);
  form.append("note", $("#studioNote").value.trim());
  form.append("duration", $("#studioDuration").value);
  form.append("ratio", $("#studioRatio").value);
  form.append("seed", String(Math.max(0, Number($("#studioSeed").value) || 0)));
  form.append("title", `网页应用 · ${STUDIO_KIND_LABEL[studioKind]}`);
  if (studioKind === "image") {
    const first = $("#studioFirstFrame").files[0], last = $("#studioLastFrame").files[0];
    if (!first) throw new Error("首帧 / 首尾帧模式必须添加首帧");
    form.append("first_frame", first, first.name);
    if (last) form.append("last_frame", last, last.name);
  }
  if (studioKind === "reference") {
    if (!referenceCount()) throw new Error("多参考模式至少需要一个参考素材");
    studioReferences.images.forEach(item => form.append("images", item.file, item.file.name));
    studioReferences.videos.forEach(item => form.append("videos", item.file, item.file.name));
    studioReferences.audios.forEach(item => form.append("audios", item.file, item.file.name));
    form.append("video_audio_flags", studioReferences.videos.map(item => item.useAudio ? "1" : "0").join(","));
  }
  return form;
}

function renderStudioTask(task) {
  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const resultStage = $("#studioResultStage");
  $(".studio-result").classList.toggle("is-complete", task.status === "completed");
  resultStage.classList.toggle("is-generating", ["dispatching", "running"].includes(task.status));
  resultStage.classList.toggle("is-complete", task.status === "completed");
  resultStage.classList.toggle("portrait", task.ratio === "9:16");
  resultStage.classList.toggle("landscape", task.ratio !== "9:16");
  $("#studioTaskState").textContent = statusLabel(task.status);
  $("#studioTaskId").textContent = task.id;
  $("#studioProgressValue").textContent = `${progress}%`;
  $("#studioProgressBar").style.width = `${progress}%`;
  $("#studioTaskMessage").textContent = task.error || task.message || "任务正在处理";
  const artifact = task.artifacts?.find(item => item.media === "video") || task.artifacts?.[0];
  const complete = task.status === "completed" && artifact?.download_url;
  $("#studioResultActions").classList.toggle("hidden", !complete);
  const video = $("#studioVideo");
  const empty = $("#studioResultEmpty");
  if (complete) {
    $("#studioDownload").href = artifact.download_url;
    empty.classList.add("hidden");
    const signedUrlExpired = Number(video.dataset.urlExpiresAt || 0) <= Date.now();
    if (video.dataset.taskId !== task.id || signedUrlExpired) {
      video.preload = "none";
      video.poster = artifact.poster_url || "";
      video.src = artifact.preview_url || artifact.download_url;
      video.dataset.taskId = task.id;
      video.dataset.urlExpiresAt = String(Date.now() + Number(artifact.download_url_expires_in || 3600) * 1000);
    }
    video.classList.remove("hidden");
  } else {
    video.pause();
    video.removeAttribute("src");
    video.removeAttribute("data-task-id");
    video.removeAttribute("data-url-expires-at");
    video.load();
    video.classList.add("hidden");
    empty.classList.remove("hidden");
    const active = ["queued", "dispatching", "running"].includes(task.status);
    empty.querySelector("strong").textContent = active ? "任务正在生成" : task.status === "failed" ? "任务生成失败" : "暂无可预览视频";
    empty.querySelector("small").textContent = active ? "你可以查看其他任务，当前任务会继续运行" : (task.error || task.message || "该任务没有可播放的结果");
  }
}

function studioTaskIsActive(task) {
  return ["queued", "dispatching", "running"].includes(task.status);
}

function scheduleStudioTaskRefresh(delay = STUDIO_ACTIVE_REFRESH_MS) {
  clearTimeout(studioPollTimer);
  if (
    !studioTaskItems.some(studioTaskIsActive)
    || $("#page-studio").classList.contains("hidden")
  ) return;
  studioPollTimer = setTimeout(refreshStudioTaskCenter, delay);
}

function stopStudioTaskEvents() {
  if (studioEventSource) studioEventSource.close();
  studioEventSource = null;
}

function watchStudioTask(task) {
  if (!task || !studioTaskIsActive(task) || typeof EventSource === "undefined") {
    stopStudioTaskEvents();
    return;
  }
  if (studioEventSource?.taskId === task.id) return;
  stopStudioTaskEvents();
  const source = new EventSource(`/api/account/tasks/${encodeURIComponent(task.id)}/events`);
  source.taskId = task.id;
  studioEventSource = source;
  source.addEventListener("task", event => {
    const updated = JSON.parse(event.data);
    const index = studioTaskItems.findIndex(item => item.id === updated.id);
    if (index >= 0) studioTaskItems[index] = updated;
    else studioTaskItems.unshift(updated);
    renderStudioTaskList(studioTaskItems);
    if (studioSelectedTaskId === updated.id) renderStudioTask(updated);
    if (!studioTaskIsActive(updated)) {
      stopStudioTaskEvents();
      Promise.allSettled([refreshAccount(), loadCalls(), refreshStudioTaskCenter()]);
    }
  });
  source.onerror = () => {
    stopStudioTaskEvents();
    scheduleStudioTaskRefresh();
  };
}

function renderStudioTaskList(items) {
  const host = $("#studioTaskList");
  const activeCount = items.filter(studioTaskIsActive).length;
  $("#studioTaskSummary").textContent = `${items.length} 个任务${activeCount ? ` · ${activeCount} 个进行中` : ""}`;
  if (!items.length) {
    host.innerHTML = '<div class="task-center-empty">还没有通过网页应用创建任务</div>';
    return;
  }
  const actions = host.closest(".studio-task-center")?.querySelector(".task-center-actions");
  if (actions && !actions.querySelector(".task-carousel-controls")) {
    const controls = document.createElement("span"); controls.className = "task-carousel-controls";
    for (const [label, direction] of [["向左浏览", -1], ["向右浏览", 1]]) {
      const button = document.createElement("button"); button.type = "button"; button.className = "task-carousel-button";
      button.textContent = direction < 0 ? "←" : "→"; button.setAttribute("aria-label", label);
      button.addEventListener("click", () => host.scrollBy({left:direction * Math.max(host.clientWidth * .8, 260), behavior:"smooth"}));
      controls.append(button);
    }
    actions.prepend(controls);
  }
  host.innerHTML = items.slice(0, 6).map(task => {
    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const selected = task.id === studioSelectedTaskId;
    const promptPreview = task.note || task.prompt_preview || STUDIO_KIND_LABEL[task.kind] || "未命名任务";
    const completed = task.status === "completed";
    const activeClass = task.status === "running" ? " is-running" : task.status === "dispatching" ? " is-dispatching" : "";
    const taskProgress = completed ? 100 : progress;
    return `<article class="studio-task-card${selected ? " selected" : ""}${completed ? " is-complete" : ""}${activeClass}">
      <button class="studio-task-main" type="button" data-studio-task="${esc(task.id)}">
        <span class="task-card-thumb" aria-hidden="true"><span>▶</span><i>${completed ? "视频" : esc(statusLabel(task.status))}</i></span>
        <span class="task-card-copy"><span class="task-card-head"><strong class="task-prompt" title="${esc(promptPreview)}">${esc(promptPreview)}</strong><span class="task-mini-state ${esc(task.status)}">${esc(statusLabel(task.status))}</span></span>
        <span class="task-card-meta">${esc(STUDIO_KIND_LABEL[task.kind] || task.kind)} · ${esc(task.quality)} · ${Number(task.duration)} 秒 · ${esc(task.ratio)}</span>
        <span class="task-mini-progress"><i style="width:${taskProgress}%"></i></span></span>
      </button>
      <span class="task-card-actions"><button class="task-note-edit" type="button" data-note-studio-task="${esc(task.id)}" aria-label="${task.note ? "修改" : "添加"}视频备注" title="${task.note ? "修改备注" : "添加备注"}">${task.note ? "改备注" : "备注"}</button><button class="task-delete" type="button" data-delete-studio-task="${esc(task.id)}" aria-label="删除任务 ${esc(task.id.slice(-8))}" title="删除任务">删除</button></span>
    </article>`;
  }).join("");
  host.querySelectorAll("[data-studio-task]").forEach(button => button.addEventListener("click", () => selectStudioTask(button.dataset.studioTask)));
  host.querySelectorAll("[data-delete-studio-task]").forEach(button => button.addEventListener("click", () => deleteStudioTask(button.dataset.deleteStudioTask, button)));
  host.querySelectorAll("[data-note-studio-task]").forEach(button => button.addEventListener("click", () => editStudioTaskNote(button.dataset.noteStudioTask, button)));
  items.slice(0, 6).forEach((task, index) => {
    const card = host.querySelector(`[data-studio-task="${CSS.escape(task.id)}"]`)?.closest(".studio-task-card");
    if (!card?.isConnected) return;
    if ("IntersectionObserver" in window) {
      studioPosterTasks.set(card, task);
      studioPosterObserver.observe(card);
    } else loadStudioTaskPoster(task, card);
  });
}

const studioPosterTasks = new WeakMap();
const studioPosterObserver = "IntersectionObserver" in window ? new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const card = entry.target, task = studioPosterTasks.get(card);
    if (task) loadStudioTaskPoster(task, card);
    studioPosterObserver.unobserve(card);
  }
}, {rootMargin:"240px 0px"}) : null;

function loadStudioTaskPoster(task, card) {
  if (!card || task.status !== "completed" || card.dataset.posterLoaded) return;
  const artifact = task.artifacts?.find(item => item.media === "video"), url = artifact?.poster_url;
  const thumb = card.querySelector(".task-card-thumb");
  if (!url || !thumb) return;
  card.dataset.posterLoaded = "true";
  const image = document.createElement("img"); image.src = url; image.alt = ""; image.loading = "lazy"; image.decoding = "async";
  image.onerror = () => image.remove(); thumb.prepend(image);
}

async function editStudioTaskNote(taskId, button) {
  const task = studioTaskItems.find(item => item.id === taskId);
  const nextNote = prompt("给这条视频添加备注（最多 80 个字）", task?.note || "");
  if (nextNote === null) return;
  const note = nextNote.trim();
  if (note.length > 80) {
    toast("备注不能超过 80 个字");
    return;
  }
  button.disabled = true;
  try {
    const updated = await api(`/api/account/tasks/${encodeURIComponent(taskId)}/note`, {
      method:"PATCH",
      body:JSON.stringify({note}),
    });
    const index = studioTaskItems.findIndex(item => item.id === taskId);
    if (index >= 0) studioTaskItems[index] = {...studioTaskItems[index], note:updated.note};
    if (studioSelectedTaskDetail?.id === taskId) {
      studioSelectedTaskDetail = {...studioSelectedTaskDetail, note:updated.note};
    }
    renderStudioTaskList(studioTaskItems);
    toast(note ? "备注已保存" : "备注已清除");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

function resetStudioResult() {
  stopStudioTaskEvents();
  const resultStage = $("#studioResultStage");
  $(".studio-result").classList.remove("is-complete");
  resultStage.classList.remove("is-generating", "is-complete", "portrait");
  resultStage.classList.add("landscape");
  const video = $("#studioVideo");
  video.pause();
  video.removeAttribute("src");
  video.removeAttribute("data-task-id");
  video.removeAttribute("data-url-expires-at");
  video.load();
  video.classList.add("hidden");
  const empty = $("#studioResultEmpty");
  empty.classList.remove("hidden");
  empty.querySelector("strong").textContent = "生成结果会出现在这里";
  empty.querySelector("small").textContent = "提交后可以离开本页，任务会继续运行";
  $("#studioTaskState").textContent = "准备就绪";
  $("#studioTaskId").textContent = "尚未创建任务";
  $("#studioProgressValue").textContent = "0%";
  $("#studioProgressBar").style.width = "0%";
  $("#studioTaskMessage").textContent = "选择生成方式和清晰度，填写描述后创建视频。";
  $("#studioResultActions").classList.add("hidden");
}

async function deleteStudioTask(taskId, button) {
  const task = studioTaskItems.find(item => item.id === taskId);
  if (task && studioTaskIsActive(task)) {
    toast("任务正在生成，请先取消后再删除");
    return;
  }
  if (!confirm("删除后将不再显示这条任务，确认删除？")) return;
  button.disabled = true;
  try {
    await api(`/api/account/tasks/${encodeURIComponent(taskId)}`, {method:"DELETE"});
    const wasSelected = studioSelectedTaskId === taskId;
    studioTaskItems = studioTaskItems.filter(item => item.id !== taskId);
    if (wasSelected) {
      studioSelectedTaskDetail = null;
      studioSelectedTaskId = studioTaskItems.find(studioTaskIsActive)?.id || studioTaskItems[0]?.id || "";
      if (studioSelectedTaskId) localStorage.setItem("equaiStudioTaskId", studioSelectedTaskId);
      else localStorage.removeItem("equaiStudioTaskId");
    }
    renderStudioTaskList(studioTaskItems);
    if (wasSelected && studioSelectedTaskId) await selectStudioTask(studioSelectedTaskId);
    else if (wasSelected) resetStudioResult();
    await Promise.allSettled([loadCalls(), refreshAccount()]);
    toast("任务已删除");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

async function selectStudioTask(taskId) {
  if (studioSelectedTaskDetail?.id !== taskId) studioSelectedTaskDetail = null;
  studioSelectedTaskId = taskId;
  localStorage.setItem("equaiStudioTaskId", taskId);
  renderStudioTaskList(studioTaskItems);
  try {
    const task = await api(`/api/account/tasks/${encodeURIComponent(taskId)}`);
    if (studioSelectedTaskId === taskId) {
      studioSelectedTaskDetail = task;
      renderStudioTaskList(studioTaskItems);
      renderStudioTask(task);
      watchStudioTask(task);
    }
  } catch (error) {
    if (studioSelectedTaskId === taskId) $("#studioTaskMessage").textContent = error.message;
  }
}

async function refreshStudioTaskCenter() {
  clearTimeout(studioPollTimer);
  const refreshButton = $("#studioRefreshTasks");
  refreshButton.disabled = true;
  try {
    const data = await api("/api/account/tasks?limit=100");
    studioTaskItems = data.items.filter(task => String(task.title || "").startsWith("网页应用 ·"));
    if (!studioTaskItems.some(task => task.id === studioSelectedTaskId)) {
      studioSelectedTaskDetail = null;
      studioSelectedTaskId = studioTaskItems.find(studioTaskIsActive)?.id || studioTaskItems[0]?.id || "";
      if (studioSelectedTaskId) localStorage.setItem("equaiStudioTaskId", studioSelectedTaskId);
      else localStorage.removeItem("equaiStudioTaskId");
    }
    renderStudioTaskList(studioTaskItems);
    if (studioSelectedTaskId) {
      const selected = await api(`/api/account/tasks/${encodeURIComponent(studioSelectedTaskId)}`);
      if (selected.id === studioSelectedTaskId) {
        studioSelectedTaskDetail = selected;
        renderStudioTask(selected);
        watchStudioTask(selected);
      }
    }
    void refreshAccount().catch(() => null);
    // Keep every task card fresh. The event stream only follows the selected
    // task and can remain open without delivering data through some clients.
    scheduleStudioTaskRefresh();
  } catch (error) {
    $("#studioTaskList").innerHTML = `<div class="task-center-empty">${esc(error.message)}，稍后自动重试</div>`;
    $("#studioTaskMessage").textContent = `${error.message}，稍后会自动重试。`;
    if (!$("#page-studio").classList.contains("hidden")) studioPollTimer = setTimeout(refreshStudioTaskCenter, 8000);
  } finally {
    refreshButton.disabled = false;
  }
}

function resumeStudioTask() {
  refreshStudioTaskCenter();
}

$("#studioRefreshTasks").addEventListener("click", refreshStudioTaskCenter);
$$('[data-studio-scroll]').forEach(button => button.addEventListener("click", () => {
  const list = $("#studioTaskList");
  const direction = Number(button.dataset.studioScroll) || 1;
  list.scrollBy({left: direction * Math.max(220, list.clientWidth * 0.78), behavior: "smooth"});
}));

$("#videoAppForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#studioSubmit");
  $("#studioFormError").textContent = "";
  try {
    const form = buildStudioFormData();
    const fingerprint = studioFingerprint(form);
    if (!studioSubmission || studioSubmission.fingerprint !== fingerprint) {
      studioSubmission = {fingerprint, key:`web-${crypto.randomUUID()}`};
    }
    button.disabled = true;
    button.querySelector("span").textContent = "正在上传并创建";
    $("#studioResultActions").classList.add("hidden");
    $("#studioVideo").pause();
    $("#studioVideo").classList.add("hidden");
    $("#studioResultEmpty").classList.remove("hidden");
    $("#studioTaskState").textContent = "正在提交";
    $("#studioTaskMessage").textContent = "素材正在安全上传，提交完成前请保持页面开启。";
    const quality = studioQuality();
    const submissionForm = await prepareStudioDirectUpload(form, progress => {
      button.querySelector("span").textContent = `素材直传 ${progress}%`;
      $("#studioTaskState").textContent = "正在上传";
      $("#studioProgressValue").textContent = `${progress}%`;
      $("#studioProgressBar").style.width = `${progress}%`;
      $("#studioTaskMessage").textContent = `素材已直传 ${progress}%，完成前请保持页面开启。`;
    });
    const submissionKey = studioSubmission.key;
    // Do not keep the form blocked while the API validates/stages the already
    // uploaded assets. The task-create request is safe to finish in the
    // background because it carries the same idempotency key.
    const createPromise = uploadForm(
      `/api/account/videos/${quality}`,
      submissionForm,
      {"Idempotency-Key":submissionKey},
    );
    $("#studioTaskState").textContent = "已创建";
    $("#studioProgressValue").textContent = "100%";
    $("#studioProgressBar").style.width = "100%";
    $("#studioTaskMessage").textContent = "素材上传成功，任务已创建，压缩、分析和生成将在后台继续。";
      toast("素材上传完成，正在创建任务");
    button.disabled = false;
    button.querySelector("span").textContent = "创建视频";
    void createPromise.then(async task => {
      studioSelectedTaskId = task.id;
      studioSelectedTaskDetail = task;
      localStorage.setItem("equaiStudioTaskId", task.id);
      studioSubmission = null;
      renderStudioTask(task);
      watchStudioTask(task);
      await refreshAccount();
      await loadCalls();
      await refreshStudioTaskCenter();
    }).catch(async error => {
      $("#studioFormError").textContent = `后台创建失败：${error.message}`;
      $("#studioTaskState").textContent = "创建失败";
      $("#studioTaskMessage").textContent = "素材已经上传，但任务创建失败。可再次点击创建，系统会使用同一个请求标识避免重复扣费。";
      await refreshStudioTaskCenter().catch(() => null);
    });
    return;
  } catch (error) {
    $("#studioFormError").textContent = error.message;
    $("#studioTaskState").textContent = "未创建";
    $("#studioTaskMessage").textContent = error.message;
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "创建视频";
  }
});

setStudioKind("text");
updateStudioPrice();

function callsMarkup(items) {
  if (!items.length) return '<div class="empty">还没有调用记录</div>';
  const rows = items.map(item => {
    const commerceTask = item.title === "电商复刻";
    const promptPreview = item.note || (commerceTask ? item.title : item.prompt_preview || item.title) || "未命名任务";
    const taskError = commerceTask && item.error ? "生成失败，请稍后重试。" : item.error;
    const toolLabel = commerceTask ? "电商复刻" : item.tool === "remix" ? "remix（支持视频编辑）" : item.tool === "image_edit" ? "图片编辑" : STUDIO_KIND_LABEL[item.kind] || item.kind;
    const created = item.created_at ? new Date(item.created_at * 1000).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "—";
    return `<tr>
    <td>${created}</td>
    <td>${esc(toolLabel)}</td>
    <td class="call-task-cell"><span class="call-prompt" title="${esc(promptPreview)}">${esc(promptPreview)}</span><span class="call-task-meta"><code title="${esc(item.id)}">${esc(item.id.slice(-8))}</code> · ${item.tool === "image_edit" ? "图片" : `${esc(item.quality)} · ${Number(item.duration)}s`}</span></td>
    <td><span class="status ${esc(item.status)}">${esc(statusLabel(item.status))}</span>${taskError ? `<br><span class="danger">${esc(taskError.slice(0, 90))}</span>` : ""}</td>
    <td>${Number(item.charged).toFixed(3)} pt<br><span class="muted">冻结 ${Number(item.price).toFixed(3)}</span></td>
    <td class="call-result-cell">
      <div class="call-result-actions">
        <button class="btn small task-detail" type="button" data-task="${esc(item.id)}">查看</button>
        <button class="btn primary small task-download" type="button" data-task="${esc(item.id)}" ${item.status === "completed" && !item.output_expired && item.artifacts?.length ? "" : "disabled"}>下载</button>
      </div>
      <span class="call-result-expiry">${item.output_expired ? "文件已清理" : (item.output_expires_at ? `保留至 ${stamp(item.output_expires_at)}` : "")}</span>
    </td>
  </tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr><th>创建时间</th><th>应用</th><th>任务</th><th>状态</th><th>消耗积分</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
let callItems = [];
function callAppId(item) {
  const value = `${item.tool || ""} ${item.kind || ""} ${item.title || ""}`.toLowerCase();
  if (/commerce|ecommerce|电商复刻/.test(value)) return "commerce-remix";
  if (/digital.?human|数字人/.test(value)) return "digital-human";
  if (/music|audio|音乐/.test(value)) return "music";
  if (/image.?edit|图片编辑/.test(value)) return "image-edit";
  if (/video.?edit|remix|视频编辑/.test(value)) return "editor";
  return "studio";
}
function filteredCallItems() {
  const query = $("#callSearch")?.value.trim().toLocaleLowerCase() || "";
  const app = $("#callAppFilter")?.value || "";
  const daysValue = $("#callDateFilter")?.value || "7";
  const cutoff = daysValue === "all" ? 0 : Date.now() / 1000 - Number(daysValue) * 86400;
  return callItems.filter(item => {
    const searchable = `${item.title || ""} ${item.note || ""} ${item.prompt_preview || ""} ${item.tool || ""} ${item.kind || ""} ${item.id || ""}`.toLocaleLowerCase();
    return (!query || searchable.includes(query))
      && (!app || callAppId(item) === app)
      && (!cutoff || Number(item.created_at || 0) >= cutoff);
  });
}
function renderCalls() {
  const items = filteredCallItems();
  $("#callTable").innerHTML = callsMarkup(items);
  $("#callResultCount").textContent = `共 ${items.length} 条`;
  $$(".task-detail").forEach(btn => btn.addEventListener("click", () => showTask(btn.dataset.task)));
  $$(".task-download").forEach(btn => btn.addEventListener("click", () => downloadTask(btn.dataset.task, btn)));
}
async function loadCalls() {
  const filter = $("#callFilter").value;
  const data = await api(`/api/account/tasks?limit=200${filter ? `&status=${encodeURIComponent(filter)}` : ""}`);
  callItems = data.items;
  renderCalls();
}
$("#callFilter").addEventListener("change", loadCalls);
$("#callSearch").addEventListener("input", renderCalls);
$("#callAppFilter").addEventListener("change", renderCalls);
$("#callDateFilter").addEventListener("change", renderCalls);
$("#callRefresh").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await loadCalls(); } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("#callExport").addEventListener("click", () => {
  const rows = filteredCallItems();
  if (!rows.length) { toast("当前筛选下没有可导出的记录"); return; }
  const fields = ["创建时间", "应用", "任务", "状态", "消耗积分", "任务编号"];
  const quote = value => `"${String(value ?? "").replaceAll('"', '""')}"`;
  const lines = [fields, ...rows.map(item => [
    item.created_at ? new Date(item.created_at * 1000).toLocaleString("zh-CN", {hour12:false}) : "",
    ({studio:"视频生成", "commerce-remix":"电商复刻", "digital-human":"数字人", editor:"视频编辑", "image-edit":"图片编辑", music:"音乐编辑"})[callAppId(item)],
    item.note || item.prompt_preview || item.title || "",
    statusLabel(item.status), Number(item.charged).toFixed(3), item.id,
  ])].map(row => row.map(quote).join(",")).join("\r\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob(["\ufeff", lines], {type:"text/csv;charset=utf-8"}));
  link.download = `EQUAI-调用记录-${new Date().toISOString().slice(0,10)}.csv`;
  link.click(); URL.revokeObjectURL(link.href);
});

async function downloadTask(id, button) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "准备下载…";
  try {
    const item = await api(`/api/account/tasks/${encodeURIComponent(id)}`);
    const artifact = item.artifacts?.find(value => value.media === "video" && value.download_url)
      || item.artifacts?.find(value => value.download_url);
    if (!artifact?.download_url) {
      throw new Error(item.output_expired ? "视频已清理，无法下载" : "视频暂时不可下载");
    }
    const link = document.createElement("a");
    link.href = artifact.download_url;
    link.download = artifact.name || "video.mp4";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("已开始下载");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function showTask(id) {
  try {
    const item = await api(`/api/account/tasks/${encodeURIComponent(id)}`);
    const pendingPrompt = ["queued", "dispatching"].includes(item.status);
    taskDetailPrompts = item.title === "电商复刻" ? {
      original: "原视频与参考图片",
      processed: "系统自动生成",
    } : {
      original: item.original_prompt || item.prompt_preview || "历史任务未记录原始提示词",
      processed: item.processed_prompt || (item.tool === "image_edit" ? "图片编辑直接使用原始要求，不进行提示词扩写。" : pendingPrompt ? "提示词正在处理中…" : "历史任务未记录处理后的提示词"),
    };
    $("#taskDetailTitle").textContent = item.note || item.title || `任务 ${item.id.slice(-8)}`;
    $("#taskDetailMeta").textContent = `${statusLabel(item.status)} · ${item.tool === "image_edit" ? "图片编辑" : item.quality + " · " + item.duration + " 秒"} · 运行 ${duration(item.run_seconds)} · 扣分 ${Number(item.charged).toFixed(3)} pt`;
    $("#taskOriginalPrompt").textContent = taskDetailPrompts.original;
    $("#taskProcessedPrompt").textContent = taskDetailPrompts.processed;
    const download = $("#taskDetailDownload");
    download.textContent = item.tool === "image_edit" ? "下载图片" : "下载视频";
    const downloadUrl = item.artifacts?.[0]?.download_url || "";
    download.classList.toggle("hidden", !downloadUrl);
    download.href = downloadUrl || "#";
    $("#taskDetailModal").classList.remove("hidden");
  } catch (error) { toast(error.message); }
}

function closeTaskDetail() { $("#taskDetailModal").classList.add("hidden"); }
$("#closeTaskDetail").addEventListener("click", closeTaskDetail);
$("#taskDetailDone").addEventListener("click", closeTaskDetail);
$("#taskDetailModal").addEventListener("click", event => {
  if (event.target === event.currentTarget) closeTaskDetail();
});
$$('.copy-task-prompt').forEach(button => button.addEventListener("click", async () => {
  await navigator.clipboard.writeText(taskDetailPrompts[button.dataset.promptKind] || "");
  toast("提示词已复制");
}));

async function loadKeys() {
  const data = await api("/api/account/keys");
  $("#keyList").innerHTML = data.keys.length ? data.keys.map(key => `<div class="key-card"><div><strong>${esc(key.name)}</strong><br><code>${esc(key.prefix)}••••••••</code><br><span class="muted">创建 ${stamp(key.created_at)} · 最近调用 ${stamp(key.last_used_at)}</span></div><div><span class="status ${esc(key.status)}">${esc(statusLabel(key.status))}</span> ${key.status !== "revoked" ? `<button class="btn small danger-btn revoke-key" data-key="${esc(key.id)}">撤销</button>` : ""}</div></div>`).join("") : '<div class="empty">尚未创建 API Key</div>';
  $$(".revoke-key").forEach(btn => btn.addEventListener("click", async () => { if (!confirm("撤销后无法恢复，确认继续？")) return; await api(`/api/account/keys/${btn.dataset.key}`, {method:"DELETE"}); await loadKeys(); }));
}
$("#createKeyBtn").addEventListener("click", async () => {
  const name = prompt("给这个 Key 起个名字", "production"); if (!name) return;
  try { const result = await api("/api/account/keys", {method:"POST", body:JSON.stringify({name})}); showSecret("保存 API Key", result.api_key, result.notice); await loadKeys(); } catch (error) { toast(error.message); }
});

async function loadLedger() {
  try {
    const data = await api(`/api/account/ledger?limit=${ledgerLimit}`);
    $("#ledgerTable").innerHTML = data.items.length ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>任务</th><th>可用变动</th><th>实际扣除</th><th>余额</th></tr></thead><tbody>${data.items.map(item => `<tr><td>${stamp(item.created_at)}</td><td>${esc(item.type)}<br><span class="muted">${esc(item.note)}</span></td><td><code>${esc(item.task_id || "—")}</code></td><td class="${item.available_delta < 0 ? "danger" : "signal"}">${item.available_delta > 0 ? "+" : ""}${Number(item.available_delta).toFixed(3)}</td><td>${Number(item.charged).toFixed(3)}</td><td>${Number(item.available_after).toFixed(3)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">还没有积分变动</div>';
    $("#ledgerMore").classList.toggle("hidden", data.items.length < ledgerLimit || ledgerLimit >= 200);
  } catch (error) {
    $("#ledgerTable").innerHTML = `<div class="notice">积分明细暂时无法载入：${esc(error.message)}</div>`;
    $("#ledgerMore").classList.add("hidden");
  }
}
$("#ledgerMore").addEventListener("click", () => { ledgerLimit = Math.min(200, ledgerLimit + 20); loadLedger(); });

async function loadDocs() {
  if (docsLoaded) return;
  const host = $("#embeddedDocs");
  try {
    const response = await fetch("/docs.html?copy=public-safe-20260905", {credentials:"same-origin", cache:"no-store"});
    if (!response.ok) throw new Error(`文档载入失败 (${response.status})`);
    const documentCopy = new DOMParser().parseFromString(await response.text(), "text/html");
    const reference = documentCopy.querySelector(".reference-main");
    if (!reference) throw new Error("文档内容无效");
    reference.querySelector(".reference-footer")?.remove();
    host.replaceChildren(reference);
    host.querySelectorAll(".runtime-origin").forEach(el => el.textContent = location.origin);
    host.querySelectorAll(".copy-code").forEach(button => button.addEventListener("click", async () => {
      const code = button.closest(".code-card")?.querySelector("code")?.innerText || "";
      await navigator.clipboard.writeText(code);
      toast("代码已复制");
    }));
    host.querySelectorAll('[data-copy="origin"]').forEach(button => button.addEventListener("click", async () => {
      await navigator.clipboard.writeText(location.origin);
      toast("接口基址已复制");
    }));
    docsLoaded = true;
  } catch (error) {
    host.innerHTML = `<div class="notice"><strong>接口文档暂时无法载入</strong><br>${esc(error.message)}，请刷新页面后重试。</div>`;
  }
}

const isLocalPreview = /^(localhost|127\.0\.0\.1)$/.test(location.hostname)
  && new URLSearchParams(location.search).has("local-preview");
if (!isLocalPreview) api("/api/account/me").then(enterConsole).catch(() => {});
