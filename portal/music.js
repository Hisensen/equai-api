(() => {
  const active = new Set(["music_queued", "music_dispatching", "music_running", "awaiting_lyrics"]);
  let refreshTimer = 0;
  let refreshing = false;
  const el = (tag, text = "", className = "") => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  if (typeof APP_PAGES !== "undefined") APP_PAGES.add("music");

  async function refreshAccess() {
    const card = $("#musicAppCard"), page = $("#page-music"), submit = $("#musicForm button[type=submit]");
    if (!card || !page) return false;
    try {
      const status = await api("/api/account/music/status");
      card.classList.toggle("hidden", !status.available);
      page.classList.toggle("hidden", !status.available && !page.classList.contains("hidden"));
      if (status.available) {
        $(".apps-count").textContent = "6 个创作工具";
        $("#musicPrice").textContent = `每首歌 ${Number(status.price)} 积分，成功后扣除。`;
        submit.disabled = false;
      } else {
        $(".apps-count").textContent = "5 个创作工具";
      }
      return Boolean(status.available);
    } catch {
      card.classList.add("hidden");
      $(".apps-count").textContent = "5 个创作工具";
      return false;
    }
  }

  async function refreshTasks() {
    const target = $("#musicTasks");
    if (!target || refreshing) return;
    refreshing = true;
    clearTimeout(refreshTimer);
    try {
      const data = await api("/api/account/music/tasks?limit=50");
      target.replaceChildren();
      if (!data.items?.length) {
        target.append(el("div", "提交后，歌曲会显示在这里。", "empty"));
        return;
      }
      for (const task of data.items) {
        const card = el("article", "", "creative-task");
        const title = el("h3", task.title || "音乐生成");
        const state = task.status === "completed" ? "已完成"
          : task.status === "failed" ? "失败"
          : task.status === "awaiting_lyrics" ? "等待确认歌词"
          : task.status === "music_queued" ? "排队中"
          : task.status === "music_dispatching" ? "正在提交 GPU"
          : task.status === "music_running" ? "生成中" : "处理中";
        card.append(title, el("p", `${state} · ${task.message || ""}`));
        if (task.error) card.append(el("p", task.error, "form-error"));
        if (task.can_edit_lyrics) {
          const lyrics = el("textarea");
          lyrics.rows = 6; lyrics.maxLength = 20000; lyrics.value = task.lyrics || "";
          lyrics.setAttribute("aria-label", "确认或修改识别歌词");
          const confirm = el("button", "确认歌词并生成", "btn primary small");
          confirm.type = "button";
          confirm.addEventListener("click", async () => {
            confirm.disabled = true;
            try {
              await api(`/api/account/music/tasks/${encodeURIComponent(task.id)}/lyrics`, {
                method: "PATCH", body: JSON.stringify({lyrics: lyrics.value}),
              });
              await api(`/api/account/music/tasks/${encodeURIComponent(task.id)}/generate`, {method: "POST"});
              toast("歌词已确认，开始生成");
              await refreshTasks();
            } catch (error) {
              toast(error.message); confirm.disabled = false;
            }
          });
          card.append(lyrics, confirm);
        }
        for (const artifact of task.artifacts || []) {
          const link = el("a", `下载 ${artifact.name || "音频"}`, "btn small");
          link.href = artifact.download_url || "#"; link.target = "_blank"; link.rel = "noopener";
          card.append(link);
        }
        target.append(card);
      }
      if (data.items.some(task => active.has(task.status))) refreshTimer = setTimeout(refreshTasks, 4000);
    } catch (error) {
      target.replaceChildren(el("div", error.message, "empty"));
    } finally {
      refreshing = false;
    }
  }

  $("#musicForm")?.addEventListener("submit", event => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const file = $("#musicSource").files[0], error = $("#musicError");
    const button = formElement.querySelector('[type="submit"]');
    if (!file) { error.textContent = "请选择音频文件"; return; }
    const body = new FormData();
    body.append("kind", "reference");
    body.append("audios", file, file.name);
    body.append("style", $("#musicStyle").value);
    body.append("voice", $("#musicVoice").value);
    body.append("prompt", $("#musicPrompt").value);
    body.append("lyrics", $("#musicLyrics").value);
    body.append("title", file.name);
    button.disabled = true; error.textContent = "";
    prepareStudioDirectUpload(body)
      .then(staged => uploadForm("/api/account/music", staged, {"Idempotency-Key": crypto.randomUUID()}))
      .then(() => { toast("音乐任务已创建"); formElement.reset(); refreshTasks(); })
      .catch(err => { error.textContent = err.message; })
      .finally(() => { button.disabled = false; });
  });

  $("#musicRefresh")?.addEventListener("click", refreshTasks);
  document.addEventListener("creative-page", event => {
    if (event.detail === "music") { window.scrollTo(0, 0); refreshTasks(); }
  });
  const observer = new MutationObserver(() => {
    if (!document.body.classList.contains("auth-mode")) refreshAccess();
  });
  observer.observe(document.body, {attributes: true, attributeFilter: ["class"]});
  refreshAccess();
})();
