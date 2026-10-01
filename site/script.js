// Install tabs (Linux/macOS vs Windows) and copy-to-clipboard buttons.
// The page is fully usable without JavaScript; this only adds conveniences.
(() => {
  const tabs = document.querySelectorAll(".tabs [role=tab]");
  const panels = document.querySelectorAll(".code[data-panel]");
  const show = (name) => {
    tabs.forEach(t => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    panels.forEach(p => { p.hidden = p.dataset.panel !== name; });
  };
  tabs.forEach(t => t.addEventListener("click", () => show(t.dataset.tab)));
  if (/Windows/i.test(navigator.userAgent)) show("win");

  document.querySelectorAll(".copy").forEach(btn => {
    btn.addEventListener("click", async () => {
      const text = btn.parentElement.querySelector("code").innerText;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Copied ✓";
      } catch {
        btn.textContent = "Select & copy";
      }
      setTimeout(() => { btn.textContent = "Copy"; }, 1800);
    });
  });
})();
