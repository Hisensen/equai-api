// Local-only view switch for visual QA when this static preview has no API server.
// It never runs on EQUAI domains and does not create an authenticated session.
(() => {
  if (!/^(localhost|127\.0\.0\.1)$/.test(location.hostname)) return;
  const view = new URLSearchParams(location.search).get("local-preview");
  if (!view) return;
  const pageNames = {
    overview: "overview",
    apps: "apps",
    studio: "studio",
    "commerce-remix": "commerce-remix",
    music: "music",
    "digital-human": "digital-human",
    remix: "remix",
    remix2: "remix2",
    editor: "editor",
    "image-edit": "image-edit"
  };
  const page = pageNames[view] || "overview";
  document.body.classList.remove("auth-mode", "landing-active", "auth-form-active");
  document.querySelector("#loginLanding")?.classList.add("hidden");
  document.querySelector("#authView")?.classList.add("hidden");
  document.querySelector("#consoleView")?.classList.remove("hidden");
  document.querySelectorAll("#consoleView .page").forEach(section => {
    section.classList.toggle("hidden", section.id !== `page-${page}`);
  });
  document.querySelectorAll("#consoleView [data-page]").forEach(button => {
    button.classList.toggle("active", button.dataset.page === (page === "overview" ? "overview" : page === "api" ? "api" : "apps"));
  });
  window.__EQUAI_LOCAL_PREVIEW__ = true;
  if (page === "studio") {
    document.querySelector('[data-studio-kind="reference"]')?.click();
    const stage = document.querySelector("#studioResultStage");
    const poster = document.createElement("img");
    poster.className = "studio-local-poster";
    poster.src = "/login-stills/eagle.webp";
    poster.alt = "本地界面示意画面";
    stage?.prepend(poster);
    const tasks = document.querySelector("#studioTaskList");
    if (tasks) tasks.innerHTML = [
      ["云端之上", "eagle.webp", "H3 · 1080p · 5 秒"],
      ["晨光山峦", "savanna.webp", "H3 · 1080p · 6 秒"],
      ["流动的云海", "white.webp", "H3 · 720p · 4 秒"],
      ["林间微光", "forest.webp", "H3 · 720p · 5 秒"]
    ].map(([title, image, meta]) => `<article class="studio-preview-work"><img src="/login-stills/${image}" alt=""><div><strong>${title}</strong><small>${meta}</small></div><span>示例</span></article>`).join("");
    const summary = document.querySelector("#studioTaskSummary");
    if (summary) summary.textContent = "界面示例 · 可左右滑动浏览";
  }
  if (page === "overview") {
    const sample = {balance:"376.800", reserved:"12.000", completed:"128", active:"2"};
    document.querySelector("#balance")?.replaceChildren(sample.balance);
    document.querySelector("#reserved")?.replaceChildren(sample.reserved);
    document.querySelector("#completedCount")?.replaceChildren(sample.completed);
    document.querySelector("#activeCount")?.replaceChildren(sample.active);
    document.querySelector("#callResultCount")?.replaceChildren("共 128 条");
    const examples = [
      ["10-05 18:20", "视频生成", "云端之上", "completed", "成功", "12.000"],
      ["10-05 18:12", "图片编辑", "商品背景替换", "completed", "成功", "3.000"],
      ["10-05 18:08", "数字人", "产品介绍口播", "running", "进行中", "12.000"],
      ["10-05 17:56", "电商复刻", "香水宣传视频", "completed", "成功", "18.000"],
      ["10-05 17:40", "视频编辑", "场景调整", "failed", "失败", "0.000"],
      ["10-05 17:22", "音乐编辑", "背景音乐改编", "completed", "成功", "6.000"],
    ];
    const table = document.querySelector("#callTable");
    if (table) table.innerHTML = `<div class="table-wrap"><table><thead><tr><th>创建时间</th><th>应用</th><th>任务</th><th>状态</th><th>消耗积分</th><th>操作</th></tr></thead><tbody>${examples.map(([date, app, task, state, status, points]) => `<tr><td>${date}</td><td>${app}</td><td class="call-task-cell"><span class="call-prompt">${task}</span><span class="call-task-meta">720p · 5s</span></td><td><span class="status ${state}">${status}</span></td><td>${points} pt</td><td><button class="btn small" type="button" disabled>查看</button></td></tr>`).join("")}</tbody></table></div>`;
  }
})();
