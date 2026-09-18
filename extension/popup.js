async function render() {
  const { enabled, state, stats = {} } = await chrome.storage.local.get(["enabled", "state", "stats"]);
  document.getElementById("state").textContent = state || "–";
  document.getElementById("checks").textContent = (stats.checks || 0).toLocaleString();
  document.getElementById("errors").textContent = stats.errors || 0;
  document.getElementById("last").textContent = stats.last ? `${Math.round((Date.now() - stats.last) / 1000)}s ago` : "–";
  document.getElementById("toggle").textContent = enabled ? "Stop" : "Start";
}
document.getElementById("toggle").addEventListener("click", async () => {
  const { enabled } = await chrome.storage.local.get(["enabled"]);
  await chrome.storage.local.set({ enabled: !enabled });
  if (!enabled) chrome.runtime.sendMessage({ type: "mutfeeder:kick" });
  render();
});
render();
setInterval(render, 1000);
