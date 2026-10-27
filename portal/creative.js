/* Separate commerce tools, sharing account authentication and task history. */
(() => {
  let activePage = "", timer = 0, epoch = 0, refreshing = false, refreshRequested = false, taskRefreshUntil = 0;
  const views = {remix: {list: "#remixTasks", tool: "remix"}, remix2: {list: "#remix2Tasks", tool: "remix"}, "commerce-remix": {list: "#commerceRemixTasks", tool: "commerce_remix"}, "digital-human": {list: "#digitalHumanTasks", tool: "digital_human"}, editor: {list: "#editorTasks", tool: "reenact"}, "image-edit": {list: "#imageEditTasks", tool: "image_edit"}};
  const urls = new Map(), submissions = new Map(), createdTasks = new Map();
  const posterTasks = new WeakMap();
  const posterObserver = "IntersectionObserver" in window ? new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const card = entry.target, task = posterTasks.get(card);
      if (task) loadCreativePoster(task, card);
      posterObserver.unobserve(card);
    }
  }, {rootMargin:"240px 0px"}) : null;
  let editorPricePer15Seconds = 4, editPrice15 = 2, reenactPrice15 = 4;
  let digitalHumanPricePerSegment = 1, digitalHumanAvailable = false;
  let imageEditConfig = null, imageEditModel = "gpt-image-2", imageEditAspectRatio = "auto";
  const TEMPLATES_ENABLED = false;  // template library is shelved for now
  const editorMode = () => "reenact";  // one pipeline: the system decides from the Prompt
  function updateDigitalHumanEstimate(seconds) {
    const valid = Number.isFinite(seconds) && seconds >= 1 && seconds <= 60;
    const duration = valid ? seconds : 0;
    const completeWindows = duration ? Math.floor(duration / 15) : 0;
    const tail = duration - completeWindows * 15;
    // Match digital_human_render_duration()/digital_human_segment_count() in the API:
    // a tail shorter than one second after a full 15-second window is discarded.
    const billableDuration = completeWindows > 0 && tail > 0 && tail < 1
      ? completeWindows * 15
      : duration;
    const segments = duration ? Math.max(1, Math.ceil(billableDuration / 15)) : 0;
    $("#digitalHumanPrice").textContent = valid
      ? `${duration.toFixed(2)} 秒 · 每 15 秒 ${digitalHumanPricePerSegment.toFixed(3)} pt · 本次预计 ${(segments * digitalHumanPricePerSegment).toFixed(3)} pt（${segments} 段）；失败退还积分。`
      : `每 15 秒 ${digitalHumanPricePerSegment.toFixed(3)} pt，不足 15 秒按 1 段计；生成失败退还积分。`;
    return valid;
  }
  $("#digitalHumanImage").addEventListener("change", event => previewImages(event.currentTarget, $("#digitalHumanImagePreview")));
  $("#digitalHumanAudio").addEventListener("change", event => {
    const file = event.currentTarget.files[0];
    if (!file) return;
    if (file.size > 300 * 1024 ** 2) { $("#digitalHumanError").textContent = "音频最大 300 MB"; return; }
    const audio = new Audio(), url = URL.createObjectURL(file);
    audio.preload = "metadata"; audio.src = url;
    audio.onloadedmetadata = () => { URL.revokeObjectURL(url); const valid = updateDigitalHumanEstimate(audio.duration); $("#digitalHumanAudioInfo").textContent = valid ? `音频 ${audio.duration.toFixed(2)} 秒 · 最终视频保留完整原始上传声音。` : "音频须为 1–60 秒。"; };
    audio.onerror = () => { URL.revokeObjectURL(url); $("#digitalHumanAudioInfo").textContent = "无法读取音频时长，请换用 MP3、WAV、M4A 或 FLAC。"; };
  });
  const remixTemplates = {
    person: {label:"只换人物", roles:["person"]},
    product: {label:"只换商品", roles:["product"]},
    scene: {label:"只换场景", roles:["scene"]},
    person_product: {label:"人物＋商品", roles:["person", "product"]},
    person_scene: {label:"人物＋场景", roles:["person", "scene"]},
    product_scene: {label:"商品＋场景", roles:["product", "scene"]},
    all: {label:"人物＋商品＋场景", roles:["person", "product", "scene"]},
  };
  const remixRoleNames = {person:"人物", product:"商品", scene:"场景"};
  const isActive = task => ["queued", "dispatching", "running"].includes(task.status);
  const safeUrl = value => { try { const url = new URL(value, location.origin); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } };
  function createIdempotencyKey() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  const musicErrorMessage = error => error?.status === 401
    ? "登录已失效，请重新登录后使用音乐编辑。"
    : error?.message || "音乐服务暂时不可用，请稍后重试。";

  function galleryStage(target) {
    const section = target?.closest(".creative-results");
    if (!section) return null;
    let stage = section.querySelector(":scope > .creative-gallery-stage");
    if (!stage) {
      stage = el("div", undefined, "creative-gallery-stage");
      stage.innerHTML = '<div class="creative-gallery-head"><strong>最新作品</strong><span>选择下方卡片查看</span></div><div class="creative-gallery-canvas"><div class="creative-gallery-empty">完成的作品会显示在这里</div></div><div class="creative-gallery-actions"></div>';
      section.insertBefore(stage, target);
    }
    return stage;
  }
  function fitGalleryVideo(canvas, player, media) {
    canvas._creativeVideoResizeObserver?.disconnect();
    if (canvas._creativeVideoResizeHandler) window.removeEventListener("resize", canvas._creativeVideoResizeHandler);
    const fit = () => {
      const sourceWidth = media.videoWidth, sourceHeight = media.videoHeight;
      const stageWidth = canvas.parentElement?.clientWidth || canvas.clientWidth;
      const maxHeight = Number.parseFloat(getComputedStyle(canvas).maxHeight) || canvas.clientHeight;
      if (!sourceWidth || !sourceHeight || !stageWidth || !maxHeight) return;
      const scale = Math.min(stageWidth / sourceWidth, maxHeight / sourceHeight);
      canvas.style.width = `${Math.floor(sourceWidth * scale)}px`;
      canvas.style.height = `${Math.floor(sourceHeight * scale)}px`;
      canvas.style.minHeight = "0";
      canvas.style.maxHeight = "none";
      canvas.style.aspectRatio = `${sourceWidth} / ${sourceHeight}`;
      canvas.style.marginInline = "auto";
      player.style.width = "100%";
      player.style.height = "100%";
    };
    media.addEventListener("loadedmetadata", fit);
    if (media.readyState >= 1) fit();
    if (window.ResizeObserver) {
      canvas._creativeVideoResizeObserver = new ResizeObserver(fit);
      canvas._creativeVideoResizeObserver.observe(canvas.parentElement || canvas);
    } else {
      canvas._creativeVideoResizeHandler = fit;
      window.addEventListener("resize", canvas._creativeVideoResizeHandler, {passive:true});
    }
  }
  function addCarouselControls(section, target) {
    const head = section?.querySelector(".panel-head");
    if (!head || head.querySelector(".task-carousel-controls")) return;
    const controls = el("div", undefined, "task-carousel-controls");
    for (const [label, direction] of [["向左浏览", -1], ["向右浏览", 1]]) {
      const button = el("button", direction < 0 ? "←" : "→", "task-carousel-button");
      button.type = "button"; button.setAttribute("aria-label", label);
      button.onclick = () => target.scrollBy({left: direction * Math.max(target.clientWidth * .8, 260), behavior:"smooth"});
      controls.append(button);
    }
    head.append(controls);
  }
  async function previewCreativeTask(task, card, button) {
    const target = views[activePage] && $(views[activePage].list), stage = galleryStage(target);
    if (!stage) return;
    $$(".creative-task.is-selected").forEach(item => item.classList.remove("is-selected"));
    card.classList.add("is-selected");
    const canvas = stage.querySelector(".creative-gallery-canvas"), actions = stage.querySelector(".creative-gallery-actions");
    canvas._creativeVideoResizeObserver?.disconnect();
    canvas._creativeVideoResizeObserver = null;
    if (canvas._creativeVideoResizeHandler) window.removeEventListener("resize", canvas._creativeVideoResizeHandler);
    canvas._creativeVideoResizeHandler = null;
    canvas.classList.remove("is-image-preview");
    for (const property of ["width", "height", "min-height", "max-height", "aspect-ratio", "margin-inline"]) canvas.style.removeProperty(property);
    canvas.innerHTML = '<div class="creative-gallery-empty">正在载入作品…</div>'; actions.replaceChildren(); button.disabled = true;
    try {
      const detail = await api(`/api/account/tasks/${encodeURIComponent(task.id)}`);
      const artifacts = Array.isArray(detail.artifacts)
        ? detail.artifacts.filter(item => item && typeof item === "object")
        : [];
      const artifact = artifacts.find(item => item.media === "video" || item.media === "image") || artifacts[0];
      if (!artifact || !["video", "image"].includes(artifact.media)) {
        throw new Error(detail.status === "failed"
          ? (detail.error || detail.message || "任务失败，没有可预览的作品")
          : "任务暂时没有可播放的作品文件");
      }
      const originalUrl = safeUrl(artifact?.download_url);
      const url = safeUrl(artifact?.preview_url) || originalUrl;
      if (detail.output_expired) throw new Error("作品文件已过期，请重新生成");
      if (!url) throw new Error("作品文件暂时无法预览，请刷新后重试");
      const media = document.createElement(artifact.media === "image" ? "img" : "video");
      if (artifact.media === "image") {
        canvas.classList.add("is-image-preview");
        media.alt = detail.note || detail.title || "图片编辑结果";
        media.src = url;
        canvas.replaceChildren(media);
        const imageArtifacts = artifacts.filter(item => item.media === "image");
        if (imageArtifacts.length > 1) {
          canvas.classList.add("is-multiple-image-preview");
          const strip = el("div", undefined, "image-edit-output-strip");
          imageArtifacts.forEach((item, index) => {
            const thumb = el("button", undefined, "image-edit-output-thumb"); thumb.type = "button";
            thumb.setAttribute("aria-label", `查看生成图片 ${index + 1}`);
            thumb.setAttribute("aria-pressed", String(index === 0));
            const imageUrl = safeUrl(item.preview_url) || safeUrl(item.download_url);
            const image = document.createElement("img"); image.src = imageUrl; image.alt = `生成图片 ${index + 1}`; image.loading = "lazy";
            thumb.append(image); thumb.addEventListener("click", () => {
              if (!imageUrl) return;
              media.src = imageUrl;
              media.alt = `生成图片 ${index + 1}`;
              for (const other of strip.querySelectorAll("button")) {
                const selected = other === thumb;
                other.setAttribute("aria-current", String(selected));
                other.setAttribute("aria-pressed", String(selected));
              }
            });
            thumb.setAttribute("aria-current", String(index === 0)); strip.append(thumb);
          });
          canvas.append(strip);
        }
      } else {
        media.controls = true; media.playsInline = true; media.preload = "metadata";
        media.setAttribute("controlslist", "nodownload");
        media.poster = safeUrl(artifact.poster_url);
        media.src = url;
        const player = el("div", undefined, "creative-gallery-video-wrap");
        const play = el("button", "▶　播放视频", "creative-gallery-play");
        play.type = "button"; play.setAttribute("aria-label", "播放视频");
        play.onclick = async () => {
          try {
            if (media.paused || media.ended) await media.play();
            else media.pause();
          } catch (error) {
            toast("视频暂时无法播放，请稍后重试");
          }
        };
        const syncPlayButton = () => {
          const paused = media.paused || media.ended;
          play.hidden = !paused;
          play.setAttribute("aria-label", paused ? "播放视频" : "暂停视频");
        };
        media.addEventListener("play", syncPlayButton);
        media.addEventListener("pause", syncPlayButton);
        media.addEventListener("ended", syncPlayButton);
        media.addEventListener("error", () => toast("视频预览加载失败，请重新选择作品"), {once:true});
        player.append(media, play);
        canvas.replaceChildren(player);
        fitGalleryVideo(canvas, player, media);
        media.load();
      }
      const title = el("span", detail.note || detail.title || detail.prompt_preview || "生成结果", "creative-gallery-caption");
      actions.append(title);
      const imageArtifacts = artifacts.filter(item => item.media === "image");
      if (artifact.media === "image" && imageArtifacts.length > 1) {
        imageArtifacts.forEach((item, index) => {
          const link = el("a", `下载 ${index + 1}`, "btn small"), href = safeUrl(item.download_url);
          if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener"; actions.append(link); }
        });
      } else {
        const download = el("a", artifact.media === "image" ? "下载图片" : "下载视频", "btn small");
        download.href = originalUrl; download.target = "_blank"; download.rel = "noopener"; actions.append(download);
      }
      card.closest(".creative-results").dataset.selectedTask = task.id;
    } catch (error) { canvas.innerHTML = `<div class="creative-gallery-empty">${esc(error.message)}</div>`; }
    finally { button.disabled = false; }
  }
  function loadCreativePoster(task, card) {
    if (!card || task.status !== "completed" || task.output_expired || card.dataset.posterLoaded) return;
    const artifact = task.artifacts?.find(item => item.media === "video" || item.media === "image");
    const url = safeUrl(artifact?.poster_url || artifact?.preview_url);
    if (!url) return;
    card.dataset.posterLoaded = "true";
    const thumb = card.querySelector(".creative-task-thumb");
    const image = document.createElement("img"); image.src = url; image.alt = ""; image.loading = "lazy"; image.decoding = "async";
    image.onerror = () => { image.remove(); card.dataset.posterLoaded = ""; };
    thumb.prepend(image);
  }
  function observeCreativePoster(task, card) {
    if (!card || task.status !== "completed" || task.output_expired) return;
    posterTasks.set(card, task);
    if (posterObserver) posterObserver.observe(card);
    else loadCreativePoster(task, card);
  }
  function clearUrls(key) { (urls.get(key) || []).forEach(URL.revokeObjectURL); urls.set(key, []); }
  function previewImages(input, target) {
    clearUrls(input.id); target.replaceChildren();
    [...input.files].forEach((file, i) => {
      const url = URL.createObjectURL(file); urls.get(input.id).push(url);
      const figure = document.createElement("figure"), img = document.createElement("img");
      img.src = url; img.alt = `图${i + 1}：${file.name}`; figure.append(img);
      target.append(figure);
    });
  }
  function selectedRemixTemplate() { return $("#remixTemplate").value || "person"; }
  function updateRemixTemplate() {
    const template = remixTemplates[selectedRemixTemplate()] || remixTemplates.person;
    $$('[data-remix-role]').forEach(input => {
      const role = input.dataset.remixRole, card = input.closest("[data-role-card]"), status = card.querySelector("[data-role-status]");
      const required = template.roles.includes(role);
      if (!required && input.files[0]) {
        input.value = "";
        card.querySelector(".creative-thumb").replaceChildren();
      }
      const selected = Boolean(input.files[0]);
      card.hidden = !required;
      input.required = required;
      card.classList.toggle("is-required", required);
      card.classList.toggle("has-file", selected);
      status.textContent = selected ? "已上传" : "必选";
    });
  }
  $("#remixTemplate").addEventListener("change", updateRemixTemplate);
  $$('[data-remix-role]').forEach(input => input.addEventListener("change", () => { previewImages(input, input.parentElement.querySelector(".creative-thumb")); updateRemixTemplate(); }));
  function editingFiles() { return [...$("#editImages").files]; }
  function renderImageEditOptions(value) {
    imageEditConfig = value || null;
    const models = Array.isArray(value?.models) ? value.models : [];
    const modelTarget = $("#editModels"); modelTarget.replaceChildren();
    if (!models.length) modelTarget.append(el("p", "图片模型暂不可用，请刷新后重试。", "muted"));
    if (!models.some(item => item.id === imageEditModel)) imageEditModel = models[0]?.id || "gpt-image-2";
    for (const model of models) {
      const card = el("button", undefined, "image-edit-model-card"); card.type = "button";
      card.setAttribute("role", "radio"); card.setAttribute("aria-checked", String(model.id === imageEditModel));
      card.dataset.model = model.id;
      const copy = el("span", undefined, "image-edit-model-copy");
      copy.append(el("strong", model.display_name || model.id), el("small", model.description || ""));
      const price = el("span", undefined, "image-edit-model-price");
      const sitePrice = Number(model.site_price_points);
      price.append(el("strong", `${Number.isFinite(sitePrice) ? sitePrice.toFixed(2) : "—"} pt`), el("small", "积分 / 张"));
      card.append(copy, price);
      card.addEventListener("click", () => {
        imageEditModel = model.id;
        for (const item of modelTarget.querySelectorAll(".image-edit-model-card")) {
          item.setAttribute("aria-checked", String(item.dataset.model === imageEditModel));
        }
        updateImageEditEstimate();
      });
      modelTarget.append(card);
    }
    const ratioTarget = $("#editAspectRatios"); ratioTarget.replaceChildren();
    const ratios = Array.isArray(value?.aspect_ratios) ? value.aspect_ratios : ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "16:9", "9:16", "21:9", "9:21"];
    if (!ratios.includes(imageEditAspectRatio)) imageEditAspectRatio = "auto";
    for (const ratio of ratios) {
      const button = el("button", ratio === "auto" ? "自动" : ratio, "image-edit-ratio");
      button.type = "button"; button.setAttribute("aria-pressed", String(ratio === imageEditAspectRatio));
      button.addEventListener("click", () => {
        imageEditAspectRatio = ratio;
        for (const item of ratioTarget.querySelectorAll(".image-edit-ratio")) item.setAttribute("aria-pressed", String(item.dataset.ratio === ratio));
        button.dataset.ratio = ratio; updateImageEditEstimate();
      });
      button.dataset.ratio = ratio; ratioTarget.append(button);
    }
    $("#editCount").max = String(value?.max_count || 20);
    $("#editImages").dataset.maxImages = String(value?.max_reference_images || 10);
    $("#editPrompt").maxLength = Number(value?.max_prompt_chars || 20000);
    updateImageEditEstimate();
  }
  function updateImageEditEstimate() {
    const count = Math.max(1, Number($("#editCount").value) || 1);
    const model = imageEditConfig?.models?.find(item => item.id === imageEditModel);
    $("#editCountValue").textContent = `${count} 张`;
    if (!imageEditConfig) return;
    const sitePrice = Number(model?.site_price_points);
    $("#editPrice").textContent = Number.isFinite(sitePrice)
      ? `本次预计冻结 ${(sitePrice * count).toFixed(3)} pt（${model?.tier || "所选模型"} ${sitePrice.toFixed(2)} pt/张）；失败张数退分。`
      : "图片编辑暂不可用，请稍后重试。";
  }
  function previewEditing() {
    const target = $("#editSourcePreview"); clearUrls("editing"); target.replaceChildren();
    const files = editingFiles();
    $("#editImageCount").textContent = `${files.length} / ${Number($("#editImages").dataset.maxImages || 10)}`;
    files.forEach((file, index) => {
      const url = URL.createObjectURL(file); urls.get("editing").push(url);
      const figure = document.createElement("figure"), image = document.createElement("img"), caption = document.createElement("figcaption");
      image.src = url; image.alt = file.name;
      caption.textContent = `图${index + 1} · ${file.name}`;
      const remove = el("button", "移除", "image-edit-remove"); remove.type = "button";
      remove.setAttribute("aria-label", `移除参考图 ${file.name}`);
      remove.addEventListener("click", () => {
        const data = new DataTransfer(); files.forEach((item, itemIndex) => { if (itemIndex !== index) data.items.add(item); });
        $("#editImages").files = data.files; previewEditing();
      });
      figure.append(image, caption, remove); target.append(figure);
    });
    if (!files.length) target.append(el("p", "不上传图片时，将根据提示词直接生成。", "muted"));
  }
  $("#editImages").addEventListener("change", () => {
    const files = editingFiles(), max = Number($("#editImages").dataset.maxImages || 10);
    if (files.length > max) { $("#imageEditError").textContent = `参考图片最多 ${max} 张`; $("#editImages").value = ""; previewEditing(); return; }
    const oversized = files.find(file => file.size > 20 * 1024 ** 2);
    if (oversized) { $("#imageEditError").textContent = `“${oversized.name}”超过 20 MB`; $("#editImages").value = ""; previewEditing(); return; }
    $("#imageEditError").textContent = ""; previewEditing();
  });
  $("#editPrompt").addEventListener("input", () => { $("#editPromptCount").textContent = `${$("#editPrompt").value.length} / 20000`; });
  $("#editCount").addEventListener("input", updateImageEditEstimate);
  const editDrop = $(".image-edit-dropzone");
  editDrop.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("#editImages").click(); } });
  for (const eventName of ["dragenter", "dragover"]) editDrop.addEventListener(eventName, event => { event.preventDefault(); editDrop.classList.add("is-dragging"); });
  for (const eventName of ["dragleave", "drop"]) editDrop.addEventListener(eventName, event => { event.preventDefault(); editDrop.classList.remove("is-dragging"); });
  editDrop.addEventListener("drop", event => {
    const incoming = [...(event.dataTransfer?.files || [])];
    const current = editingFiles(), max = Number($("#editImages").dataset.maxImages || 10);
    const combined = [...current, ...incoming].filter(file => file.type.startsWith("image/"));
    if (combined.length > max) { $("#imageEditError").textContent = `参考图片最多 ${max} 张`; return; }
    const oversized = combined.find(file => file.size > 20 * 1024 ** 2);
    if (oversized) { $("#imageEditError").textContent = `“${oversized.name}”超过 20 MB`; return; }
    const data = new DataTransfer(); combined.forEach(file => data.items.add(file)); $("#editImages").files = data.files;
    $("#imageEditError").textContent = ""; previewEditing();
  });
  $$("[data-clear]").forEach(button => button.addEventListener("click", () => { const input = document.getElementById(button.dataset.clear); input.value = ""; input.dispatchEvent(new Event("change")); }));
  $("#remixVideo").addEventListener("change", event => {
    clearUrls("remixVideo"); const target = $("#remixSourcePreview"); target.replaceChildren();
    $("#remixVideoInfo").textContent = "支持 16:9、9:16，5–15 秒，最大 300 MB。";
    const file = event.target.files[0]; if (!file) return;
    const video = document.createElement("video"), url = URL.createObjectURL(file);
    urls.get("remixVideo").push(url); video.controls = true; video.playsInline = true; video.preload = "metadata"; video.src = url;
    video.addEventListener("loadedmetadata", () => { $("#remixVideoInfo").textContent = `${video.duration.toFixed(2)} 秒 · ${video.videoWidth} × ${video.videoHeight} · ${(file.size / 1024 ** 2).toFixed(1)} MB`; });
    video.addEventListener("error", () => { $("#remixVideoInfo").textContent = "浏览器无法预览此文件，请使用 MP4 视频。"; });
    target.append(video);
  });
  $("#editorVideo")?.addEventListener("change", event => {
    clearUrls("editorVideo"); const target = $("#editorVideoPreview"); target.replaceChildren();
    const file = event.currentTarget.files[0]; if (!file) { target.innerHTML = '<p class="muted">选择视频后可在这里预览。</p>'; $("#editorVideoInfo").textContent = `按时长计费：每 15 秒 ${editorPricePer15Seconds} pt，不足 15 秒按一档计；例如 15 秒 2 pt、25/30 秒 4 pt。`; $("#editorForm button[type=submit]").disabled = false; return; }
    const video = document.createElement("video"), url = URL.createObjectURL(file);
    urls.get("editorVideo").push(url); video.className = "editor-preview-video"; video.controls = true; video.playsInline = true; video.preload = "metadata"; video.src = url;
    video.addEventListener("durationchange", () => {
      const seconds = video.duration, amount = Math.max(1, Math.ceil((seconds - .1) / 15)) * editorPricePer15Seconds;
      const valid = Number.isFinite(seconds) && seconds >= 4.9;
      $("#editorVideoInfo").textContent = `${seconds.toFixed(2)} 秒 · ${valid ? `本次 ${amount} pt` : "视频时长不能少于 5 秒"} · 每 15 秒 ${editorPricePer15Seconds} pt，不足 15 秒按一档计`;
      $("#editorForm button[type=submit]").disabled = !valid;
    });
    video.addEventListener("error", () => {
      const note = document.createElement("p"); note.className = "editor-preview-error";
      note.textContent = "浏览器无法解码该视频进行预览。请转为 H.264 编码的 MP4 后再试；视频仍可用于提交编辑。";
      target.replaceChildren(note);
    }, {once:true});
    target.append(video);
    video.load();
  });

  async function loadConfig() {
    try {
      const [value, music] = await Promise.all([
        api("/api/account/creative/config"),
        activePage === "music"
          ? api("/api/account/music/status").catch(error => ({available: false, price: 0, authExpired: error.status === 401}))
          : Promise.resolve(null),
      ]);
      if (!account) return;
      digitalHumanAvailable = Boolean(value.digital_human?.available);
      if (Number.isFinite(Number(value.digital_human?.price_per_segment))) digitalHumanPricePerSegment = Number(value.digital_human.price_per_segment);
      $("#digitalHumanForm button[type=submit]").disabled = !digitalHumanAvailable;
      $("#digitalHumanPrice").textContent = digitalHumanAvailable
        ? `每 15 秒 ${digitalHumanPricePerSegment.toFixed(3)} pt，不足 15 秒按 1 段计；不足 1 秒的尾段并入上一段，失败退还积分。`
        : "长视频 GPU 服务暂不可用，请稍后再试。";
      if (music) $("#musicPrice").textContent = music.available
        ? `每首歌 ${Number(music.price)} pt，成功后扣除。`
        : music.authExpired
          ? "登录已失效，请重新登录后使用音乐编辑。"
          : "音乐服务暂不可用，请稍后再试。";
      // Keep the submit control usable even while the status probe is delayed
      // or unavailable; the authenticated API returns the precise service
      // error instead of silently leaving the user with a dead button.
      $("#musicForm button[type=submit]").disabled = false;
      renderImageEditOptions(value.image_edit);
      $("#editPrice").textContent = value.image_edit.available ? $("#editPrice").textContent : "图片编辑正在配置中，暂未开放提交。";
      // A failed config probe must not leave the form looking clickable while
      // the browser silently ignores submit. The create route reports the
      // current service status and a useful error to the user.
      $("#imageEditForm button[type=submit]").disabled = false;
      for (const option of $("#remixQuality").options) {
        const price = value.remix?.prices?.[option.value];
        if (Number.isFinite(Number(price))) option.textContent = `${option.value === "2k" ? "2K" : option.value.toUpperCase()} · ${Number(price)} pt`;
      }
      const remix2Price = value.remix2?.prices?.["720p"];
      if (Number.isFinite(Number(remix2Price))) {
        $("#remix2Price").textContent = `本次需要 ${Number(remix2Price)} pt`;
      }
      const editorPrices = value.video_editor?.prices;
      if (Number.isFinite(Number(editorPrices?.per_15_seconds))) editPrice15 = Number(editorPrices.per_15_seconds);
      if (Number.isFinite(Number(value.video_editor?.reenact_prices?.per_15_seconds))) reenactPrice15 = Number(value.video_editor.reenact_prices.per_15_seconds);
      editorPricePer15Seconds = editorMode() === "reenact" ? reenactPrice15 : editPrice15;
      const selectedEditorVideo = $("#editorVideoPreview video");
      if (selectedEditorVideo?.readyState >= 1 && Number.isFinite(selectedEditorVideo.duration)) {
        selectedEditorVideo.dispatchEvent(new Event("durationchange"));
      } else if (editorPrices) {
        $("#editorVideoInfo").textContent = `按时长计费：每 15 秒 ${editorPricePer15Seconds} pt，不足 15 秒按一档计。`;
      }
    } catch (error) { $("#editPrice").textContent = error.message; $("#imageEditForm button[type=submit]").disabled = false; }
  }

  async function recoverSubmission(key) {
    for (const delay of [0, 500, 1200]) {
      if (delay) await new Promise(resolve => setTimeout(resolve, delay));
      const response = await fetch(`/api/account/submissions/${encodeURIComponent(key)}`, {credentials:"same-origin"});
      if (response.status === 404) continue;
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `任务确认失败 (${response.status})`);
      return body;
    }
    return null;
  }

  function showCreatedTask(task, page) {
    const view = views[page];
    if (!account || !task?.id || !view) return;
    // The submit response is authoritative for the page that created it. Some
    // older worker paths return their internal `remix` tool name here.
    if (page === "commerce-remix") task = {...task, tool: "commerce_remix"};
    else if (!view.tool.split(",").includes(task.tool)) return;
    createdTasks.set(task.id, task);
    taskRefreshUntil = Date.now() + 90_000;
    const target = $(view.list);
    target.querySelector(".empty")?.remove();
    const card = target.querySelector(`[data-task-id="${CSS.escape(task.id)}"]`) || makeCard(task);
    updateCard(task, card);
    target.prepend(card);
  }
  function showCreativePending(page, title = "正在创建任务…", message = "正在上传素材…", progress = 4) {
    const view = views[page], target = view && $(view.list);
    if (!target) return;
    target.querySelector(".empty")?.remove();
    let card = target.querySelector('[data-task-id="creative-pending"]');
    if (!card) {
      card = el("article", undefined, "creative-task queued");
      card.dataset.taskId = "creative-pending";
      const header = el("header"); header.append(el("h3", title), el("span", "提交中", "task-mini-state queued"));
      const bar = el("div", undefined, "task-mini-progress creative-task-progress"); bar.append(document.createElement("i"));
      card.append(header, bar, el("p", message)); target.prepend(card);
    }
    card.querySelector("p").textContent = message;
    card.querySelector("i").style.width = `${Math.max(4, Math.min(100, progress || 4))}%`;
  }

  async function submit(formElement, form, path, errorElement, directUpload = false, activityLabel = "") {
    const button = formElement.querySelector('[type="submit"]'), label = button.textContent;
    if (button.disabled) return;
    button.disabled = true; button.textContent = activityLabel || label; errorElement.textContent = "";
    // Retain an idempotency key for uncertain retries; editing any input produces a new key.
    const fingerprint = JSON.stringify([...form.entries()].map(([key, value]) => [key, value instanceof File ? [value.name, value.size, value.lastModified] : value]));
    let request = submissions.get(formElement.id);
    if (!request || request.fingerprint !== fingerprint) { request = {fingerprint, key: createIdempotencyKey()}; submissions.set(formElement.id, request); }
    const page = activePage;
    try {
      const firstFile = [...form.values()].find(value => value instanceof File && value.name);
      showCreativePending(page, firstFile?.name || "数字人视频", "正在准备素材…", 4);
      const submissionForm = directUpload
        ? await prepareStudioDirectUpload(form, progress => {
            showCreativePending(page, firstFile?.name || "数字人视频", `素材直传 ${progress}%…`, progress);
            if (activityLabel) button.textContent = `正在上传素材 ${progress}%…`;
          })
        : form;
      showCreativePending(page, firstFile?.name || "数字人视频", "正在上传素材…", 4);
      if (activityLabel) button.textContent = "正在创建任务…";
      toast("正在上传素材并创建任务…");
      const task = await uploadForm(path, submissionForm, {"Idempotency-Key": request.key}, phase => {
        showCreativePending(page, firstFile?.name || "数字人视频", phase.phase === "creating" ? "正在创建任务…" : `正在上传素材 ${phase.progress ?? 0}%`, phase.progress ?? 4);
        if (activityLabel) button.textContent = phase.phase === "creating" ? "正在创建任务…" : `正在上传素材 ${phase.progress ?? 0}%…`;
      });
      submissions.delete(formElement.id);
      document.querySelector('[data-task-id="creative-pending"]')?.remove();
      showCreatedTask(task, page);
      toast(`任务已创建 · ${task.id.slice(-8)}`);
      await refreshTasks(); await refreshAccount().catch(() => null);
    } catch (error) {
      document.querySelector('[data-task-id="creative-pending"]')?.remove();
      if (activityLabel) toast(error.message);
      // Validation/signing failures are deterministic and must be shown as-is;
      // only an interrupted final POST needs idempotent recovery.
      const recoverable = /连接中断|超时|上传响应中断|请求失败 \(5\d\d\)/.test(error.message);
      if (!recoverable) {
        errorElement.textContent = error.message;
        return;
      }
      errorElement.textContent = "上传响应中断，正在确认任务是否已创建…";
      try {
        const task = await recoverSubmission(request.key);
        if (task) {
          submissions.delete(formElement.id); errorElement.textContent = "";
          showCreatedTask(task, page);
          toast(`任务已创建 · ${task.id.slice(-8)}，已自动恢复显示`);
          await refreshTasks(); await refreshAccount().catch(() => null);
        } else {
          errorElement.textContent = error.message;
          await refreshTasks();
        }
      } catch (confirmationError) { errorElement.textContent = `${error.message}；${confirmationError.message}`; }
    }
    finally { button.disabled = false; button.textContent = label; }
  }
  $("#remixForm").addEventListener("submit", event => {
    event.preventDefault(); const form = new FormData(), roles = [], file = $("#remixVideo").files[0], templateKey = selectedRemixTemplate(), template = remixTemplates[templateKey];
    if (!file || file.size > 300 * 1024 ** 2) { $("#remixError").textContent = "请上传 300 MB 以内的视频"; return; }
    for (const input of $$('[data-remix-role]')) if (input.files[0]) roles.push(input.dataset.remixRole);
    const missing = (template.roles || []).filter(role => !roles.includes(role));
    if (missing.length) { $("#remixError").textContent = `“${template.label}”需要上传${missing.map(role => remixRoleNames[role]).join("、")}图片`; document.querySelector(`[data-role-card="${missing[0]}"] input`)?.focus(); return; }
    if (!roles.length) { $("#remixError").textContent = "请至少选择一张人物、商品或场景参考图"; return; }
    form.append("kind", "reference"); form.append("videos", file); form.append("prompt", ""); form.append("remix_template", templateKey);
    form.append("remix_audio", "original");
    for (const input of $$('[data-remix-role]')) if (input.files[0]) form.append("images", input.files[0]);
    form.append("remix_roles", JSON.stringify(roles));
    submit(event.currentTarget, form, `/api/account/remix/${$("#remixQuality").value}`, $("#remixError"), true);
  });
  function updateRemix2Template() {
    const template = remixTemplates[$("#remix2Template").value] || remixTemplates.person;
    $$('[data-remix2-role]').forEach(input => {
      const role = input.dataset.remix2Role, card = input.closest("[data-remix2-role-card]"), status = card.querySelector("[data-role-status]");
      const required = template.roles.includes(role);
      if (!required && input.files[0]) { input.value = ""; card.querySelector(".creative-thumb").replaceChildren(); }
      card.hidden = !required; input.required = required; card.classList.toggle("is-required", required);
      status.textContent = input.files[0] ? "已上传" : "必选";
    });
  }
  $("#remix2Template").addEventListener("change", updateRemix2Template);
  $$('[data-remix2-role]').forEach(input => input.addEventListener("change", () => {
    previewImages(input, input.parentElement.querySelector(".creative-thumb")); updateRemix2Template();
  }));
  $("#remix2Video").addEventListener("change", event => {
    clearUrls("remix2Video"); const target = $("#remix2SourcePreview"); target.replaceChildren();
    const file = event.target.files[0]; if (!file) return;
    const video = document.createElement("video"), url = URL.createObjectURL(file);
    urls.get("remix2Video").push(url); video.controls = true; video.playsInline = true; video.preload = "metadata"; video.src = url;
    video.addEventListener("loadedmetadata", () => { $("#remix2VideoInfo").textContent = `${video.duration.toFixed(3)} 秒 · 输出将保持原时长 · ${(file.size / 1024 ** 2).toFixed(1)} MB`; });
    target.append(video);
  });
  $("#remix2Form").addEventListener("submit", event => {
    event.preventDefault(); const form = new FormData(), roles = [], file = $("#remix2Video").files[0];
    const templateKey = $("#remix2Template").value, template = remixTemplates[templateKey];
    if (!file || file.size > 300 * 1024 ** 2) { $("#remix2Error").textContent = "请上传 300 MB 以内的视频"; return; }
    $$('[data-remix2-role]').forEach(input => { if (input.files[0]) roles.push(input.dataset.remix2Role); });
    const missing = template.roles.filter(role => !roles.includes(role));
    if (missing.length) { $("#remix2Error").textContent = `“${template.label}”需要上传${missing.map(role => remixRoleNames[role]).join("、")}图片`; return; }
    form.append("kind", "reference"); form.append("videos", file); form.append("prompt", ""); form.append("remix_template", templateKey); form.append("remix_audio", "original");
    $$('[data-remix2-role]').forEach(input => { if (input.files[0]) form.append("images", input.files[0]); });
    form.append("remix_roles", JSON.stringify(roles));
    // The public API currently exposes the authenticated 720p remix route;
    // the separate remix2 path is not installed on the live API yet.
    submit(event.currentTarget, form, "/api/account/remix/720p", $("#remix2Error"), true);
  });
  $("#commerceRemixVideo").addEventListener("change", event => {
    clearUrls("commerceRemixVideo"); const target = $("#commerceRemixSourcePreview"); target.replaceChildren();
    const file = event.currentTarget.files[0];
    if (!file) { target.innerHTML = '<p class="muted">上传后可预览原片；生成时长自动跟随原片。</p>'; $("#commerceRemixVideoInfo").textContent = "最长 300 MB。"; return; }
    const video = document.createElement("video"), url = URL.createObjectURL(file);
    urls.get("commerceRemixVideo").push(url); video.controls = true; video.playsInline = true; video.preload = "metadata"; video.src = url;
    video.addEventListener("loadedmetadata", () => {
      const valid = Number.isFinite(video.duration) && video.duration >= 4.9 && video.duration <= 16;
      const aspect = video.videoWidth / video.videoHeight;
      const ratios = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1, "4:3": 4 / 3, "3:4": 3 / 4};
      const closest = Object.keys(ratios).reduce((best, item) => Math.abs(aspect / ratios[item] - 1) < Math.abs(aspect / ratios[best] - 1) ? item : best, "16:9");
      const supported = Math.abs(aspect / ratios[closest] - 1) <= .08;
      $("#commerceRemixVideoInfo").textContent = valid
        ? `${video.duration.toFixed(1)} 秒 · ${video.videoWidth}×${video.videoHeight} · ${supported ? "保留原画幅和尺寸" : "暂不支持这个画幅"}${video.duration > 15 ? " · 将使用前 15 秒" : ""}`
        : "请上传 5–15 秒的视频（不足 16 秒的片尾余量可自动截取）";
      $("#commerceRemixForm button[type=submit]").disabled = !valid || !supported;
    });
    video.addEventListener("error", () => { $("#commerceRemixVideoInfo").textContent = "浏览器无法预览该视频，请使用 MP4 视频。"; });
    target.append(video);
  });
  $$('[data-commerce-role]').forEach(input => input.addEventListener("change", () => {
    previewImages(input, input.parentElement.querySelector(".creative-thumb"));
    input.closest(".creative-role").querySelector("[data-role-status]").textContent = input.files[0] ? "已上传" : "可选";
    input.closest(".creative-role").classList.toggle("has-file", Boolean(input.files[0]));
  }));
  $("#commerceRemixForm").addEventListener("submit", event => {
    event.preventDefault();
    const form = new FormData(), file = $("#commerceRemixVideo").files[0], roles = [];
    if (!file || file.size > 300 * 1024 ** 2) { $("#commerceRemixError").textContent = "请上传 300 MB 以内的视频"; return; }
    form.append("kind", "reference"); form.append("videos", file); form.append("prompt", "");
    for (const input of $$('[data-commerce-role]')) if (input.files[0]) {
      roles.push(input.dataset.commerceRole); form.append("images", input.files[0]);
    }
    form.append("remix_roles", JSON.stringify(roles));
    const quality = $("#commerceRemixQuality").value;
    submit(event.currentTarget, form, `/api/account/commerce-remix/${quality}`, $("#commerceRemixError"), true, "正在提交…");
  });
  $("#commerceRemixQuality").addEventListener("change", event => {
    const option = event.currentTarget.selectedOptions[0];
    $("#commerceRemixPrice").textContent = `每条视频 ${option.dataset.price} 积分，成功后扣除；失败退回积分。`;
  });
  $("#digitalHumanForm").addEventListener("submit", event => {
    event.preventDefault();
    const form = new FormData(), image = $("#digitalHumanImage").files[0];
    const audio = $("#digitalHumanAudio").files[0];
    $("#digitalHumanError").textContent = "";
    if (!image || image.size > 30 * 1024 ** 2) { $("#digitalHumanError").textContent = "请上传 30 MB 以内的一张人物图"; return; }
    if (!digitalHumanAvailable) { $("#digitalHumanError").textContent = "长视频 GPU 服务暂不可用，请稍后再试。"; return; }
    if (!audio || audio.size > 300 * 1024 ** 2) { $("#digitalHumanError").textContent = "请上传 300 MB 以内的音频文件"; return; }
    // images/audios are the names the staging grant understands; the endpoint
    // accepts both spellings so the multipart fallback still works.
    form.append("kind", "reference"); form.append("mode", "audio");
    form.append("images", image); form.append("audios", audio);
    form.append("ratio", $("#digitalHumanRatio").value);
    form.append("prompt", $("#digitalHumanPrompt").value);
    submit(event.currentTarget, form, "/api/account/digital-human/720p", $("#digitalHumanError"), true);
  });
  $("#imageEditForm").addEventListener("submit", async event => {
    event.preventDefault(); const files = editingFiles(), form = new FormData();
    if (files.length > Number(imageEditConfig?.max_reference_images || 10) || files.some(file => file.size > 20 * 1024 ** 2)) { $("#imageEditError").textContent = "最多选择 10 张图片，每张不超过 20 MB"; return; }
    const prompt = $("#editPrompt").value.trim();
    if (!prompt || prompt.length > Number(imageEditConfig?.max_prompt_chars || 20000)) { $("#imageEditError").textContent = "请填写 1–20000 字的提示词"; return; }
    // Avoid decoding/re-encoding large PNGs in the browser before the upload
    // starts. The API normalizes images after direct upload, and the Tencent
    // uploader already handles large files in bounded chunks.
    for (const file of files) form.append("images", file);
    form.append("prompt", prompt);
    form.append("model", imageEditModel);
    form.append("aspect_ratio", imageEditAspectRatio);
    form.append("count", $("#editCount").value);
    // kind only tells the staging grant which media vocabulary to use; the
    // endpoint itself ignores it.
    form.append("kind", "image-edit");
    await submit(event.currentTarget, form, "/api/account/image-edits", $("#imageEditError"), true, "正在上传素材…");
  });
  $("#editorForm")?.addEventListener("submit", event => {
    event.preventDefault(); const form = new FormData(), file = $("#editorVideo").files[0], selected = [...$("#editorImages").files];
    if (!file || file.size > 300 * 1024 ** 2) { $("#editorError").textContent = "请上传 300 MB 以内的视频"; return; }
    const sourceVideo = $("#editorVideoPreview video");
    if (sourceVideo && !(sourceVideo.duration >= 4.9 && Number.isFinite(sourceVideo.duration))) { $("#editorError").textContent = "视频时长不能少于 5 秒"; return; }
    if (selected.length > 3 || selected.some(image => image.size > 30 * 1024 ** 2)) { $("#editorError").textContent = "参考图片最多 3 张，每张不超过 30 MB"; return; }
    const prompt = $("#editorPrompt").value.trim();
    if (!prompt) { $("#editorError").textContent = "请填写编辑 Prompt"; return; }
    form.append("kind", "reference"); form.append("videos", file); form.append("prompt", prompt); form.append("remix_template", "generic"); form.append("remix_audio", "original"); form.append("edit_mode", editorMode());
    selected.forEach(image => form.append("images", image));
    form.append("remix_roles", JSON.stringify(selected.map((_, index) => `reference_${index + 1}`)));
    // Use the same signed, chunked COS upload path as the working video
    // generator. The editor accepts zero to three optional reference images.
    submit(event.currentTarget, form, "/api/account/video-edits/720p", $("#editorError"), true);
  });
  document.querySelectorAll("input[name=editorMode]").forEach(input => input.addEventListener("change", () => {
    editorPricePer15Seconds = editorMode() === "reenact" ? reenactPrice15 : editPrice15;
    const hint = $("#editorModeHint");
    if (hint) hint.textContent = editorMode() === "reenact"
      ? "按原片结构和你的 Prompt 写新台词，重新选角、生成机位图、配音表演，再自动加字幕、音效和包装；全程自动质检，不合格会自动重做。"
      : "保留原视频的声音和动作，只替换人物、商品或场景。";
    $("#editorVideo")?.dispatchEvent(new Event("change"));
  }));
  $("#editorImages")?.addEventListener("change", event => {
    const target = $("#editorReferences");
    clearUrls("editorImages"); target.replaceChildren();
    [...event.currentTarget.files].forEach((file, index) => {
      const figure = document.createElement("figure"), image = document.createElement("img"), caption = document.createElement("figcaption");
      const url = URL.createObjectURL(file); urls.get("editorImages").push(url);
      figure.dataset.referenceNumber = String(index + 1);
      image.src = url; image.alt = `图 ${index + 1}`; caption.textContent = `图 ${index + 1}`;
      figure.append(image, caption); target.append(figure);
    });
  });

  async function openResult(task, card, button) {
    await previewCreativeTask(task, card, button);
  }
  async function openProject(task, card, button) {
    const region = card.querySelector(".creative-project-overview");
    if (region.dataset.loaded === "true") { region.replaceChildren(); region.dataset.loaded = "false"; button.textContent = "查看镜头项目"; return; }
    button.disabled = true;
    try {
      const project = await api(`/api/account/video-projects/${encodeURIComponent(task.project_id)}`);
      if (task.tool === "reenact") { region.replaceChildren(reenactOverview(project, task)); region.dataset.loaded = "true"; button.textContent = "收起过程"; return; }
      const title = document.createElement("strong"); title.textContent = `${project.title} · ${project.status}`;
      const list = document.createElement("ol");
      for (const node of project.nodes || []) {
        const item = document.createElement("li"), output = node.output || {};
        const start = Number.isFinite(Number(node.source_start)) ? `${Number(node.source_start).toFixed(2)}s` : "";
        const end = Number.isFinite(Number(node.source_end)) ? `${Number(node.source_end).toFixed(2)}s` : "";
        item.textContent = `${node.node_key} · ${start}${end ? `–${end}` : ""} · ${node.status}${output.job_id ? ` · ${output.job_id.slice(-8)}` : ""}`;
        list.append(item);
      }
      if (!list.children.length) { const item = document.createElement("li"); item.textContent = "正在分析视频，镜头时间线稍后出现。"; list.append(item); }
      region.replaceChildren(title, list); region.dataset.loaded = "true"; button.textContent = "收起项目";
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }
  function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
  const stepNames = {analysis: "① 看懂原片", direction: "② 导演方案", script: "③ 新台词", render: "⑦ 合成成片"};
  function reenactOverview(project, task) {
    const root = el("div", undefined, "reenact-overview");
    if (task.warnings?.length) { const warn = el("div", undefined, "reenact-warnings"); warn.append(el("strong", "质检提示")); task.warnings.forEach(w => warn.append(el("p", w))); root.append(warn); }
    const docs = project.treatment?.documents || {};
    if (task.notes?.length) { const info = el("div", undefined, "reenact-notes"); task.notes.forEach(n => info.append(el("p", n))); root.append(info); }
    for (const name of ["CHANGES", "BRIEF", "TREATMENT", "ANALYSIS", "TIMELINE", "SCRIPT"]) {
      if (!docs[name]) continue;
      const box = el("details"); box.append(el("summary", name), el("pre", docs[name])); root.append(box);
    }
    const nodes = project.nodes || [];
    const gallery = el("div", undefined, "reenact-gallery");
    for (const node of nodes.filter(n => n.node_type === "image")) {
      const out = node.output || {}, attempts = out.attempts || [];
      const fig = el("figure");
      const best = attempts.find(a => a.stored && out.stored && a.stored.key === out.stored.key) || attempts[attempts.length - 1] || {};
      const url = safeUrl(out.url || best.url || "");
      if (url) { const img = el("img"); img.src = url; img.loading = "lazy"; img.alt = node.node_key; fig.append(img); }
      const score = best.qa?.score !== undefined ? ` · 质检 ${best.qa.score}` : "";
      fig.append(el("figcaption", `${node.node_key} · 第 ${attempts.length} 次${score} · ${node.status}`));
      if (best.qa?.issues?.length) fig.append(el("small", best.qa.issues.join("；")));
      gallery.append(fig);
    }
    if (gallery.children.length) { root.append(el("h4", "④ 机位图 / 图标 / B-roll")); root.append(gallery); }
    const voices = nodes.filter(n => n.node_type === "voice");
    if (voices.length) {
      root.append(el("h4", "⑤ 音色参考"));
      voices.forEach(v => { const row = el("div", `${v.node_key} · ${v.output?.seconds || 0}s `); const url = safeUrl(v.output?.url || ""); if (url) { const audio = el("audio"); audio.controls = true; audio.src = url; row.append(audio); } root.append(row); });
    }
    const takes = nodes.filter(n => n.node_type === "take");
    if (takes.length) {
      root.append(el("h4", "⑥ 表演片段（每次生成与质检）"));
      for (const take of takes) {
        const box = el("details"); box.append(el("summary", `${take.node_key} · ${take.status} · ${(take.output?.attempts || []).length} 次`));
        for (const [i, attempt] of (take.output?.attempts || []).entries()) {
          const row = el("div", undefined, "reenact-attempt");
          row.append(el("strong", `第 ${i + 1} 次 · ${attempt.qa ? `质检 ${attempt.qa.score} 分${attempt.qa.cer !== undefined ? ` · 台词误差 ${(attempt.qa.cer * 100).toFixed(0)}%` : ""}` : attempt.error ? "失败" : "生成中"}`));
          if (attempt.error) row.append(el("p", attempt.error));
          if (attempt.qa?.issues?.length) row.append(el("p", attempt.qa.issues.join("；")));
          const url = safeUrl(attempt.url || "");
          if (url) { const video = el("video"); video.controls = true; video.preload = "none"; video.src = url; row.append(video); }
          box.append(row);
        }
        root.append(box);
      }
    }
    const steps = el("p", nodes.filter(n => stepNames[n.node_key]).map(n => `${stepNames[n.node_key]} ${n.status === "ready" ? "✓" : n.status}`).join("　"), "muted");
    root.prepend(steps);
    return root;
  }
  const hoursLeft = until => Math.max(0, Math.round((until * 1000 - Date.now()) / 3600000));
  async function loadTemplates() {
    const target = $("#editorTemplates"); if (!TEMPLATES_ENABLED || !target || !account) return;
    try {
      const {items} = await api("/api/account/video-templates");
      target.replaceChildren();
      if (!items.length) { target.innerHTML = '<div class="empty">还没有模板。在下方已完成的重演记录上点「保存为模板」。</div>'; return; }
      for (const tpl of items) target.append(templateCard(tpl));
    } catch (error) { toast(error.message); }
  }
  function templateCard(tpl) {
    const card = el("article", undefined, "creative-task template-card");
    const head = el("header"); head.append(el("h3", tpl.name), el("span", `剩 ${hoursLeft(tpl.expires_at)} 小时`, "status"));
    const summary = tpl.summary || {};
    const meta = el("p", `${summary.mode || ""} · ${summary.segments || 0} 段 · ${tpl.source_seconds ? Number(tpl.source_seconds).toFixed(1) + " 秒" : ""} · 角色：${(summary.cast || []).join("、")} · 已用 ${tpl.uses} 次`, "creative-task-meta muted");
    card.append(head, meta);
    if (tpl.note) card.append(el("p", tpl.note, "creative-task-message"));
    if (summary.style) card.append(el("small", `风格：${summary.style}`, "muted"));
    const actions = el("div", undefined, "creative-task-actions");
    const preview = el("button", "预览", "btn small"), use = el("button", "使用模板", "btn primary small"), remove = el("button", "删除", "btn small");
    [preview, use, remove].forEach(b => b.type = "button");
    actions.append(use, preview, remove); card.append(actions);
    const media = el("div", undefined, "creative-result-media"), form = el("form", undefined, "template-run hidden");
    form.innerHTML = '<label>新的提示词（每行一条，多行即批量，最多 20 条）<textarea rows="4" required placeholder="换成一个穿红色运动服的短发女生，场景换成海边\n换成两个男生，场景换成屋顶天台"></textarea></label><label>参考图片（可选，每条都会使用，最多 3 张）<input type="file" accept="image/*,.heic,.heif" multiple></label><label>备注（可选）<input type="text" maxlength="60"></label><p class="form-error" role="alert"></p><button class="btn primary" type="submit">开始生成</button>';
    card.append(form, media);
    preview.onclick = () => {
      if (media.children.length) { media.replaceChildren(); return; }
      const url = safeUrl(tpl.preview_url || ""); if (!url) { toast("预览不可用"); return; }
      const video = el("video"); video.controls = true; video.playsInline = true; video.src = url; media.append(video);
    };
    use.onclick = () => form.classList.toggle("hidden");
    remove.onclick = async () => {
      if (!window.confirm("删除这个模板？")) return;
      try { await api(`/api/account/video-templates/${encodeURIComponent(tpl.id)}`, {method:"DELETE"}); card.remove(); } catch (error) { toast(error.message); }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      const text = form.querySelector("textarea").value.trim(), files = [...form.querySelector("input[type=file]").files], error = form.querySelector(".form-error");
      const lines = text.split("\n").map(x => x.trim()).filter(Boolean);
      if (!lines.length) { error.textContent = "请至少写一行提示词"; return; }
      if (lines.length > 20) { error.textContent = "一次最多 20 条"; return; }
      if (files.length > 3) { error.textContent = "参考图片最多 3 张"; return; }
      if (!window.confirm(`将创建 ${lines.length} 条任务，每条按视频时长计费（每 15 秒 ${reenactPrice15} pt）。继续？`)) return;
      const body = new FormData(); body.append("kind", "reference"); body.append("prompts", lines.join("\n")); body.append("note", form.querySelector("input[type=text]").value.trim());
      files.forEach(file => body.append("images", file));
      const submitButton = form.querySelector("button[type=submit]"); submitButton.disabled = true; error.textContent = "";
      try {
        const staged = await prepareStudioDirectUpload(body, progress => {
          error.textContent = `参考图片直传 ${progress}%…`;
        });
        error.textContent = "正在创建任务…";
        const result = await api(`/api/account/video-templates/${encodeURIComponent(tpl.id)}/runs`, {method:"POST", body:staged});
        toast(`已创建 ${result.items.length}/${result.requested} 条任务`); form.reset(); form.classList.add("hidden"); refreshTasks(); loadTemplates();
      } catch (err) { error.textContent = err.message; }
      finally { submitButton.disabled = false; }
    };
    return card;
  }
  $("#templatesRefresh")?.addEventListener("click", () => loadTemplates());
  function updateCard(task, card) {
    card._task = task;
    const commerceTask = task.title === "电商复刻";
    card.className = `creative-task ${task.status}`;
    card.querySelector("h3").textContent = task.note || (commerceTask ? task.title : task.prompt_preview || task.title);
    const badge = card.querySelector(".task-mini-state"); badge.textContent = statusLabel(task.status); badge.className = `task-mini-state ${task.status}`;
    card.querySelector(".creative-task-meta").textContent = `${commerceTask ? task.duration + " 秒" : task.tool === "image_edit" ? "图片编辑" : task.quality + " · " + task.duration + " 秒"} · ${stamp(task.created_at)} · ${task.id.slice(-8)}`;
    card.querySelector(".creative-task-message").textContent = commerceTask
      ? task.status === "failed" ? "生成失败，请稍后重试。"
        : task.output_expired ? "文件已过期，任务记录仍保留。"
        : task.status === "completed" ? "生成完成"
        : task.status === "queued" ? "排队中"
        : "正在生成视频…"
      : task.error || (task.output_expired ? "文件已过期，任务记录仍保留。" : task.message || statusLabel(task.status));
    const transcript = card.querySelector(".creative-transcript");
    if (transcript) { transcript.hidden = !task.transcript; transcript.textContent = task.transcript ? `原片对白：${task.transcript}` : ""; }
    const progressPercent = task.status === "completed" ? 100 : Math.max(0, Math.min(100, Number(task.progress) || 0));
    card.querySelector(".task-mini-progress i").style.width = `${progressPercent}%`;
    card.querySelector(".creative-view").disabled = task.status !== "completed" || task.output_expired;
    card.querySelector(".creative-delete").disabled = isActive(task);
    card.querySelector(".creative-task-cost").textContent = isActive(task)
      ? `已冻结 ${Number(task.price).toFixed(3)} pt · 失败全额退回`
      : task.status === "failed" && !Number(task.charged)
        ? `运行 ${duration(task.run_seconds)} · 积分已全额退回`
        : `运行 ${duration(task.run_seconds)} · 扣分 ${Number(task.charged).toFixed(3)} pt`;
    const projectButton = card.querySelector(".creative-project");
    projectButton.hidden = !task.project_id;
    card.querySelector(".creative-details").hidden = commerceTask;
    card.querySelector(".creative-template").hidden = !(TEMPLATES_ENABLED && task.tool === "reenact" && task.status === "completed" && !task.output_expired);
    if (task.tool === "reenact" && card.querySelector(".creative-project-overview").dataset.loaded !== "true") projectButton.textContent = "查看制作过程";
    if (task.tool === "reenact" && task.warnings?.length && task.status === "completed") card.querySelector(".creative-task-message").textContent = `${task.message}：${task.warnings.slice(0, 2).join("；")}`;
  }
  function makeCard(task) {
    const card = document.createElement("article"); card.dataset.taskId = task.id;
    card.innerHTML = '<button type="button" class="creative-task-thumb creative-view" aria-label="预览此作品"><span>▶</span></button><div class="creative-task-copy"><header><h3></h3><span class="task-mini-state"></span></header><p class="creative-task-meta muted"></p><p class="creative-task-message"></p><div class="task-mini-progress creative-task-progress" aria-label="任务进度"><i></i></div><small class="creative-task-cost muted"></small><div class="creative-task-actions"><button type="button" class="btn small creative-project" hidden>查看镜头项目</button><button type="button" class="btn small creative-template" hidden>保存为模板</button><button type="button" class="btn small creative-details">查看描述</button><button type="button" class="btn small creative-note">修改备注</button><button type="button" class="btn small creative-delete">删除记录</button></div><p class="creative-transcript muted" hidden></p><div class="creative-project-overview"></div></div>';
    card.querySelector(".creative-view").onclick = event => openResult(card._task, card, event.currentTarget);
    card.querySelector(".creative-project").onclick = event => openProject(card._task, card, event.currentTarget);
    card.querySelector(".creative-template").onclick = async event => {
      const name = window.prompt("模板名称（例如：双人哑铃跟练·换人换景）", (task.note || "").slice(0, 60));
      if (name === null || !name.trim()) return;
      const note = window.prompt("备注（可选，最多200字）", "") ?? "";
      event.currentTarget.disabled = true;
      try { await api("/api/account/video-templates", {method:"POST", body: JSON.stringify({task_id: task.id, name: name.trim(), note})}); toast("已保存为模板，保留 3 天"); loadTemplates(); }
      catch (error) { toast(error.message); }
      finally { event.currentTarget.disabled = false; }
    };
    card.querySelector(".creative-details").onclick = () => showTask(task.id);
    card.querySelector(".creative-note").onclick = async () => { const note = window.prompt("给这条任务写个备注（最多80字）", card.querySelector("h3").textContent); if (note === null) return; if (note.length > 80) { toast("备注最多80字"); return; } try { await api(`/api/account/tasks/${task.id}/note`, {method:"PATCH", body: JSON.stringify({note})}); await refreshTasks(); } catch (error) { toast(error.message); } };
    card.querySelector(".creative-delete").onclick = async () => { if (!window.confirm("从列表中删除这条记录？这不会退还已消耗积分。")) return; try { await api(`/api/account/tasks/${task.id}`, {method:"DELETE"}); const wasSelected = card.classList.contains("is-selected"); card.remove(); if (wasSelected) { const stage = galleryStage($(views[activePage].list)); stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">这条记录已删除</div>'; stage.querySelector(".creative-gallery-actions").replaceChildren(); stage.closest(".creative-results").dataset.selectedTask = ""; } } catch (error) { toast(error.message); } };
    updateCard(task, card); return card;
  }
  async function refreshTasks() {
    clearTimeout(timer); const view = views[activePage], version = epoch;
    if (!view || !account) return;
    if (refreshing) { refreshRequested = true; return; }
    refreshing = true;
    try {
      const data = await api(`/api/account/tasks?limit=200&tool=${encodeURIComponent(view.tool)}`);
      if (version !== epoch || !account) return;
      // The API has already applied the requested tool filter. Trust that
      // result: redundant client filtering hid legacy commerce tasks whose
      // internal worker name was still `remix`.
      const items = Array.isArray(data.items) ? [...data.items] : [], target = $(view.list);
      for (const [id, task] of createdTasks) {
        if (!view.tool.split(",").includes(task.tool)) continue;
        if (items.some(item => item.id === id)) createdTasks.delete(id);
        else items.unshift(task);
      }
      const existing = new Map([...target.querySelectorAll(".creative-task")].map(card => [card.dataset.taskId, card]));
      const stage = galleryStage(target);
      addCarouselControls(target.closest(".creative-results"), target);
      target.querySelector(".empty")?.remove();
      for (const task of items) { let card = existing.get(task.id); if (card) updateCard(task, card); else { card = makeCard(task); target.append(card); } existing.delete(task.id); }
      existing.forEach(card => card.remove());
      const section = target.closest(".creative-results");
      if (section.dataset.selectedTask && !items.some(item => item.id === section.dataset.selectedTask)) {
        section.dataset.selectedTask = "";
        const stage = galleryStage(target);
        stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">原作品已不可用，选择下方卡片查看其他作品</div>';
        stage.querySelector(".creative-gallery-actions").replaceChildren();
      }
      // Insert only new cards in order, without detaching a playing video.
      let nextCard = null;
      [...items].reverse().forEach((task, reverseIndex) => {
        const card = target.querySelector(`[data-task-id="${CSS.escape(task.id)}"]`);
        if (card && card !== nextCard) target.insertBefore(card, nextCard);
        if (card) nextCard = card;
        if (card?.isConnected) observeCreativePoster(task, card);
      });
      if (!items.length) target.innerHTML = '<div class="empty">还没有任务。上传素材并提交后，结果会显示在这里。</div>';
      if (items.length && !section.dataset.selectedTask) {
        const first = items.find(item => item.status === "completed" && !item.output_expired);
        const card = first && target.querySelector(`[data-task-id="${CSS.escape(first.id)}"]`);
        if (first && card) void previewCreativeTask(first, card, card.querySelector(".creative-view"));
        else if (items.some(isActive)) stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">任务正在处理中，进度会显示在下方记录中</div>';
      }
      if (items.some(isActive) || (activePage === "commerce-remix" && Date.now() < taskRefreshUntil)) timer = setTimeout(refreshTasks, 3000);
    } catch (error) { if (version === epoch) { toast(error.message); timer = setTimeout(refreshTasks, 8000); } }
    finally {
      refreshing = false;
      const rerun = refreshRequested || version !== epoch;
      refreshRequested = false;
      if (rerun && views[activePage] && account) void refreshTasks();
    }
  }
  async function refreshMusicTasks() {
    const target = $("#musicTasks");
    if (!target || !account) return;
    try {
      const data = await api("/api/account/music/tasks?limit=50");
      const section = target.closest(".creative-results");
      let stage = section.querySelector(":scope > .creative-gallery-stage");
      if (!stage) {
        stage = el("div", undefined, "creative-gallery-stage");
        stage.innerHTML = '<div class="creative-gallery-head"><strong>最新作品</strong><span>选择下方卡片试听</span></div><div class="creative-gallery-canvas"><div class="creative-gallery-empty">完成的作品会显示在这里</div></div><div class="creative-gallery-actions"></div>';
        section.insertBefore(stage, target);
      }
      addCarouselControls(section, target);
      target.replaceChildren();
      if (section.dataset.selectedTask && !data.items.some(item => item.id === section.dataset.selectedTask)) {
        section.dataset.selectedTask = "";
        stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">原作品已不可用，选择下方卡片试听其他作品</div>';
        stage.querySelector(".creative-gallery-actions").replaceChildren();
      }
      if (!data.items?.length) { target.innerHTML = '<div class="empty">上传音频后，任务会显示在这里。</div>'; stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">上传音频后，作品会显示在这里</div>'; return; }
      for (const task of data.items) {
        const colorState = task.status === "completed" ? "completed" : task.status === "failed" ? "failed" : task.status === "awaiting_lyrics" ? "queued" : task.status.replace(/^music_/, "");
        const card = el("article", undefined, `creative-task music-task-card ${colorState}`);
        card.dataset.taskId = task.id;
        const cover = el("button", undefined, "creative-task-thumb music-task-thumb"); cover.type = "button"; cover.setAttribute("aria-label", `预览 ${task.title || "音乐作品"}`); cover.innerHTML = '<span class="music-task-play">♪</span>';
        const copy = el("div", undefined, "creative-task-copy");
        const title = el("h3", task.note || task.title || "音乐生成");
        title.title = task.note || task.title || "音乐生成";
        const state = task.status === "completed" ? "成功" : task.status === "failed" ? "失败" : task.status === "music_queued" || task.status === "queued" ? "排队中" : task.status === "music_dispatching" || task.status === "dispatching" ? "正在启动" : task.status === "awaiting_lyrics" ? "准备中" : "生成中";
        const badge = el("span", state, `task-mini-state ${colorState}`);
        const header = el("header"); header.append(title, badge);
        const status = el("p", task.message || "", "creative-task-meta");
        const progress = el("div", undefined, "task-mini-progress creative-task-progress");
        const progressFill = document.createElement("i");
        const progressPercent = task.status === "completed" ? 100 : Math.max(0, Math.min(100, Number(task.progress) || 0));
        progressFill.style.width = `${progressPercent}%`; progress.append(progressFill);
        copy.append(header, progress, status);
        if (task.error) { const error = el("p", task.error, "music-task-error"); error.title = task.error; copy.append(error); }
        if (task.can_edit_lyrics) {
          const lyrics = el("textarea"); lyrics.rows = 6; lyrics.maxLength = 20000; lyrics.value = task.lyrics || ""; lyrics.setAttribute("aria-label", "确认或修改识别歌词");
          const confirm = el("button", "确认歌词并生成", "btn primary small");
          confirm.type = "button";
          confirm.addEventListener("click", async () => {
            confirm.disabled = true;
            try {
              await api(`/api/account/music/tasks/${encodeURIComponent(task.id)}/lyrics`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({lyrics:lyrics.value})});
              await api(`/api/account/music/tasks/${encodeURIComponent(task.id)}/generate`, {method:"POST"});
              toast("歌词已确认，开始生成"); refreshMusicTasks();
      } catch (error) { toast(musicErrorMessage(error)); confirm.disabled = false; }
          });
          copy.append(lyrics, confirm);
        }
        const mp3 = (task.artifacts || []).find(artifact => /\.mp3$/i.test(artifact.name || "") && artifact.preview_url);
        cover.disabled = task.status !== "completed" || !mp3;
        cover.onclick = () => {
          const canvas = stage.querySelector(".creative-gallery-canvas"), actions = stage.querySelector(".creative-gallery-actions");
          $$(".music-task-card.is-selected").forEach(item => item.classList.remove("is-selected")); card.classList.add("is-selected");
          const player = document.createElement("audio"); player.controls = true; player.preload = "none"; player.src = safeUrl(mp3?.preview_url);
          canvas.replaceChildren(player); actions.replaceChildren(el("span", task.title || "音乐作品", "creative-gallery-caption"));
          const download = el("a", "下载 MP3", "btn small"); download.href = safeUrl(mp3?.download_url); download.rel = "noopener"; download.download = mp3?.name || "song.mp3"; actions.append(download);
          section.dataset.selectedTask = task.id;
        };
        for (const artifact of task.artifacts || []) {
          if (!/\.mp3$/i.test(artifact.name || "")) continue;
          card.dataset.previewUrl = safeUrl(artifact.preview_url);
        }
        const actionsRow = el("div", undefined, "creative-task-actions");
        const noteButton = el("button", task.note ? "改备注" : "备注", "btn small"); noteButton.type = "button";
        noteButton.onclick = async () => {
          const note = window.prompt("给这首音乐写个备注（最多 80 字）", task.note || "");
          if (note === null) return;
          if (note.trim().length > 80) { toast("备注最多 80 字"); return; }
          noteButton.disabled = true;
          try {
            await api(`/api/account/tasks/${encodeURIComponent(task.id)}/note`, {method:"PATCH", body:JSON.stringify({note:note.trim()})});
            toast("备注已保存"); await refreshMusicTasks();
          } catch (error) { toast(musicErrorMessage(error)); noteButton.disabled = false; }
        };
        const deleteButton = el("button", "删除", "btn small music-task-delete"); deleteButton.type = "button";
        deleteButton.disabled = ["queued", "dispatching", "running", "music_queued", "music_dispatching", "music_running", "awaiting_lyrics"].includes(task.status);
        deleteButton.title = deleteButton.disabled ? "任务进行中，完成后可删除" : "删除这条记录";
        deleteButton.onclick = async () => {
          if (!window.confirm("删除这条音乐记录？删除后不会退还已消耗的积分。")) return;
          deleteButton.disabled = true;
          try {
            await api(`/api/account/tasks/${encodeURIComponent(task.id)}`, {method:"DELETE"});
            if (section.dataset.selectedTask === task.id) {
              section.dataset.selectedTask = "";
              stage.querySelector(".creative-gallery-canvas").innerHTML = '<div class="creative-gallery-empty">选择下方音乐卡片试听</div>';
              stage.querySelector(".creative-gallery-actions").replaceChildren();
            }
            card.remove(); toast("记录已删除"); await refreshMusicTasks();
          } catch (error) { toast(musicErrorMessage(error)); deleteButton.disabled = false; }
        };
        actionsRow.append(noteButton, deleteButton); copy.append(actionsRow);
        if (section.dataset.selectedTask === task.id) card.classList.add("is-selected");
        card.append(cover, copy);
        target.append(card);
      }
      const selected = data.items.find(item => item.id === section.dataset.selectedTask) || data.items.find(item => item.status === "completed") || data.items[0];
      const selectedCard = target.querySelectorAll(".music-task-card")[data.items.indexOf(selected)];
      const audioTask = data.items.find(item => item.id === selected?.id);
      const audioArtifact = audioTask?.artifacts?.find(artifact => /\.mp3$/i.test(artifact.name || "") && artifact.preview_url);
      if (audioArtifact && !section.dataset.selectedTask) selectedCard?.querySelector(".creative-task-thumb")?.click();
      if (["queued", "dispatching", "running", "music_queued", "music_dispatching", "music_running"].some(s => data.items.some(t => t.status === s))) setTimeout(refreshMusicTasks, 4000);
    } catch (error) { target.textContent = ""; target.append(el("div", musicErrorMessage(error), "empty")); }
  }
  function showMusicPending(filename, message = "正在上传音频…", progress = 0) {
    const target = $("#musicTasks");
    if (!target) return null;
    target.querySelector(".empty")?.remove();
    let card = target.querySelector('[data-task-id="music-pending"]');
    if (!card) {
      card = el("article", undefined, "creative-task queued");
      card.dataset.taskId = "music-pending";
      const header = el("header"); header.append(el("h3", filename || "音乐生成"), el("span", "提交中", "task-mini-state queued"));
      const bar = el("div", undefined, "task-mini-progress creative-task-progress"); bar.append(document.createElement("i"));
      card.append(header, bar, el("p", message)); target.prepend(card);
    }
    card.querySelector("p").textContent = message;
    card.querySelector("i").style.width = `${Math.max(4, Math.min(100, progress || 4))}%`;
    return card;
  }
  $("#musicForm")?.addEventListener("submit", event => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const source = $("#musicSource").files[0], error = $("#musicError"), button = formElement.querySelector('[type="submit"]');
    if (!source) { error.textContent = "请选择音频文件"; return; }
    const form = new FormData(); form.append("kind", "reference"); form.append("audios", source, source.name); form.append("style", $("#musicStyle").value); form.append("voice", $("#musicVoice").value); form.append("prompt", $("#musicPrompt").value); form.append("lyrics", $("#musicLyrics").value); form.append("title", source.name);
    button.disabled = true; error.textContent = "";
    showMusicPending(source.name, "正在上传音频…", 4);
    const uploadKey = crypto.randomUUID();
    prepareStudioDirectUpload(form, progress => {
      showMusicPending(source.name, `音频直传 ${progress}%…`, progress);
    }).then(staged => uploadForm("/api/account/music", staged, {"Idempotency-Key": uploadKey}, phase => {
      showMusicPending(source.name, phase.phase === "creating" ? "正在创建任务…" : `正在上传音频 ${phase.progress ?? 0}%`, phase.progress ?? 0);
    })).then(task => {
      toast("音乐任务已创建");
      document.querySelector('[data-task-id="music-pending"]')?.remove();
      refreshMusicTasks();
    }).catch(e => { document.querySelector('[data-task-id="music-pending"]')?.remove(); error.textContent = musicErrorMessage(e); }).finally(() => { button.disabled = false; });
  });
  $("#musicRefresh")?.addEventListener("click", refreshMusicTasks);
  document.addEventListener("creative-page", event => {
    epoch++; activePage = event.detail; clearTimeout(timer);
    if (activePage === "music") { window.scrollTo(0, 0); loadConfig(); refreshMusicTasks(); }
    else if (views[activePage]) { window.scrollTo(0, 0); loadConfig(); refreshTasks(); if (activePage === "editor") loadTemplates(); }
  });
  $$("[data-creative-refresh]").forEach(button => button.onclick = () => refreshTasks());
  $("#logoutBtn").addEventListener("click", () => { epoch++; activePage = ""; clearTimeout(timer); submissions.clear(); createdTasks.clear(); Object.values(views).forEach(view => $(view.list).replaceChildren()); urls.forEach((_, key) => clearUrls(key)); $("#remixForm").reset(); $("#remix2Form").reset(); $("#commerceRemixForm").reset(); $("#imageEditForm").reset(); $("#remixSourcePreview").replaceChildren(); $("#remix2SourcePreview").replaceChildren(); $("#commerceRemixSourcePreview").replaceChildren(); $("#editSourcePreview").replaceChildren(); $$(".creative-thumb").forEach(el => el.replaceChildren()); });
  if (account) {
    activePage = location.hash.slice(1);
    if (activePage === "music") { loadConfig(); refreshMusicTasks(); }
    else if (views[activePage]) { loadConfig(); refreshTasks(); if (activePage === "editor") loadTemplates(); }
  }
  updateRemixTemplate();
})();
