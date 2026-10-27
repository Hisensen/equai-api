/* Voice cloning stays invisible until its independent worker is enabled. */
(() => {
  const form = document.querySelector("#voiceCloneForm");
  const card = document.querySelector("#voiceCloneAppCard");
  const target = document.querySelector("#voiceCloneTasks");
  if (!form || !card || !target) return;

  let pollTimer = 0;
  let loading = false;
  const active = new Set(["queued", "dispatching", "running"]);
  const node = (tag, content, className = "") => {
    const item = document.createElement(tag);
    item.textContent = content;
    if (className) item.className = className;
    return item;
  };

  async function refreshAvailability() {
    try {
      const status = await api("/api/account/voice-clones/status");
      window.__equaiVoiceCloneAvailable = Boolean(status.available);
      card.classList.toggle("hidden", !status.available);
      document.querySelector("#voiceClonePrice").textContent =
        `每条 ${Number(status.price).toFixed(3)} 积分；生成失败退回。`;
      applyAppDirectoryFilter();
    } catch {
      window.__equaiVoiceCloneAvailable = false;
      card.classList.add("hidden");
      applyAppDirectoryFilter();
    }
  }

  async function refreshTasks() {
    if (loading || !window.__equaiVoiceCloneAvailable) return;
    loading = true;
    clearTimeout(pollTimer);
    try {
      const data = await api("/api/account/voice-clones?limit=50");
      target.replaceChildren();
      if (!data.data?.length) {
        target.append(node("div", "提交后，生成记录会显示在这里。", "empty"));
        return;
      }
      for (const task of data.data) {
        const item = node("article", "", "creative-task");
        const label = task.status === "completed" ? "已完成" : task.status === "failed" ? "失败"
          : task.status === "queued" ? "排队中" : "生成中";
        item.append(node("h3", `声音克隆 · ${label}`),
          node("p", task.message || "", task.status === "failed" ? "form-error" : "muted"));
        if (task.status === "completed") {
          const audio = task.artifacts?.find(artifact => artifact.media === "audio" && artifact.preview_url);
          if (audio) {
            const player = document.createElement("audio");
            player.controls = true;
            player.preload = "none";
            player.src = audio.preview_url;
            player.style.width = "100%";
            item.append(player);
            const download = node("a", "下载 MP3", "btn small");
            download.href = audio.download_url || audio.preview_url;
            download.download = audio.name || "speech.mp3";
            item.append(download);
          }
        }
        target.append(item);
      }
      if (data.data.some(task => active.has(task.status))) {
        pollTimer = setTimeout(refreshTasks, 4000);
      }
    } catch (error) {
      target.replaceChildren(node("div", error.message || "记录暂时无法读取", "empty"));
    } finally {
      loading = false;
    }
  }

  document.querySelector("#voiceCloneEmotionWeight").addEventListener("input", event => {
    document.querySelector("#voiceCloneEmotionValue").textContent = Number(event.target.value).toFixed(1);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const error = document.querySelector("#voiceCloneError");
    const submit = form.querySelector("button[type=submit]");
    const voice = document.querySelector("#voiceCloneVoice").files[0];
    const emotion = document.querySelector("#voiceCloneEmotion").files[0];
    const text = document.querySelector("#voiceCloneText").value.trim();
    if (!voice || !text) { error.textContent = "请上传音色参考并填写朗读文字"; return; }
    if ([voice, emotion].some(file => file && file.size > 50 * 1024 * 1024)) {
      error.textContent = "每段参考音频不能超过 50 MB"; return;
    }
    const body = new FormData();
    body.append("voice", voice);
    if (emotion) body.append("emotion", emotion);
    body.append("text", text);
    body.append("emotion_weight", document.querySelector("#voiceCloneEmotionWeight").value);
    submit.disabled = true;
    error.textContent = "正在上传参考音频…";
    try {
      await uploadForm("/api/account/voice-clones", body,
        {"Idempotency-Key": crypto.randomUUID()}, phase => {
          error.textContent = phase.phase === "creating" ? "正在创建任务…"
            : `正在上传 ${phase.progress ?? 0}%…`;
        });
      error.textContent = "";
      toast("声音克隆任务已创建");
      await refreshTasks();
    } catch (reason) {
      error.textContent = reason.message || "任务创建失败";
    } finally {
      submit.disabled = false;
    }
  });
  document.querySelector("#voiceCloneRefresh").addEventListener("click", refreshTasks);
  document.addEventListener("creative-page", event => {
    if (event.detail === "voice-clone") {
      refreshAvailability().then(refreshTasks);
    } else {
      clearTimeout(pollTimer);
      if (event.detail === "apps" || event.detail === "overview") refreshAvailability();
    }
  });
})();
