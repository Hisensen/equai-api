(() => {
  let theme = "dark";
  try {
    theme = localStorage.getItem("equaiTheme") === "light" ? "light" : "dark";
  } catch {}
  document.documentElement.dataset.theme = theme;
})();
