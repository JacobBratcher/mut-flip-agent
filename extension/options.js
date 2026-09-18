const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["agentUrl", "token"]).then(({ agentUrl, token }) => {
  $("url").value = agentUrl || ""; $("token").value = token || "";
});
$("save").addEventListener("click", async () => {
  const agentUrl = $("url").value.trim().replace(/\/$/, ""), token = $("token").value.trim();
  let origin;
  try { origin = new URL(agentUrl).origin + "/*"; } catch { $("msg").textContent = "Enter a full URL like http://192.168.1.50:8099"; return; }
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) { $("msg").textContent = "Permission to reach the agent was denied."; return; }
  await chrome.storage.local.set({ agentUrl, token });
  try {
    const r = await fetch(agentUrl + "/config", { headers: { "X-Feeder-Token": token } });
    $("msg").textContent = r.ok ? `Connected. ${JSON.stringify(await r.json())}` : `Agent answered ${r.status}. Check the token.`;
  } catch (e) { $("msg").textContent = `Can't reach the agent: ${e}`; }
});
