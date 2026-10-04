const origin = window.location.origin;
document.querySelectorAll(".runtime-origin").forEach(node => { node.textContent = origin; });

const toast = document.querySelector("#docsToast");
function copied() {
  toast.classList.remove("hidden");
  window.setTimeout(() => toast.classList.add("hidden"), 1800);
}

document.querySelectorAll(".copy-code").forEach(button => {
  button.addEventListener("click", async () => {
    const code = button.closest(".code-card").querySelector("code").textContent;
    await navigator.clipboard.writeText(code);
    copied();
  });
});

document.querySelector('[data-copy="origin"]').addEventListener("click", async () => {
  await navigator.clipboard.writeText(origin);
  copied();
});
