// Drives the feeder: asks the agent which cards are due, has the mut.gg tab fetch them,
// and posts each result straight to the agent (which alerts Discord immediately).
const FEEDER_URL = "https://www.mut.gg/price-tracker/";
const BLOCK_CODES = new Set([0, 403, 429, 503]);
let running = false;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (keys) => chrome.storage.local.get(keys);
const set = (obj) => chrome.storage.local.set(obj);

// Settings written by install.ps1 (config.json) win, so re-running the installer updates them.
async function bootstrap() {
  try {
    const r = await fetch(chrome.runtime.getURL("config.json"));
    if (!r.ok) return;
    const c = await r.json();
    const cur = await get(["agentUrl", "token", "enabled"]);
    const patch = {};
    if (c.agentUrl && c.agentUrl !== cur.agentUrl) patch.agentUrl = c.agentUrl;
    if (c.token && c.token !== cur.token) patch.token = c.token;
    if (cur.enabled === undefined && c.autostart) patch.enabled = true;
    if (Object.keys(patch).length) await set(patch);
  } catch (_) { /* no config.json: configured via the Settings page instead */ }
}

async function agent(method, path, body) {
  const { agentUrl, token } = await get(["agentUrl", "token"]);
  const r = await fetch(agentUrl.replace(/\/$/, "") + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Feeder-Token": token || "" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`agent ${path} -> ${r.status}`);
  return r.json();
}

// Serialize tab lookups so two callers can never open two feeder tabs.
let tabLock = Promise.resolve();
function feederTab() {
  const next = tabLock.then(findOrOpenTab, findOrOpenTab);
  tabLock = next.catch(() => {});
  return next;
}

async function findOrOpenTab() {
  let { tabId } = await get(["tabId"]);
  if (!tabId) {
    const open = await chrome.tabs.query({ url: "https://www.mut.gg/*" });
    if (open.length) { tabId = open[0].id; await chrome.tabs.update(tabId, { pinned: true }); await set({ tabId }); }
  }
  if (tabId) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t.url && t.url.startsWith("https://www.mut.gg/")) {
        await chrome.tabs.sendMessage(tabId, { type: "mutfeeder:ping" });
        return tabId;
      }
    } catch (_) { /* tab gone or not ready */ }
  }
  const t = await chrome.tabs.create({ url: FEEDER_URL, pinned: true, active: false });
  await set({ tabId: t.id });
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    try {
      await chrome.tabs.sendMessage(t.id, { type: "mutfeeder:ping" });
      return t.id;
    } catch (_) { /* still loading */ }
  }
  throw new Error("mut.gg tab did not load");
}

async function bump(field, n = 1) {
  const { stats = {} } = await get(["stats"]);
  const today = new Date().toDateString();
  if (stats.day !== today) Object.assign(stats, { day: today, checks: 0, errors: 0 });
  stats[field] = (stats[field] || 0) + n;
  stats.last = Date.now();
  await set({ stats });
}

async function loop() {
  if (running) return;
  running = true;
  let backoff = 0;
  try {
    await bootstrap();
    const { agentUrl, token } = await get(["agentUrl", "token"]);
    if (!agentUrl || !token) { await set({ state: "not configured" }); return; }
    let cfg = await agent("GET", "/config");
    let cfgAt = Date.now();
    while ((await get(["enabled"])).enabled) {
      if (Date.now() - cfgAt > 600000) { cfg = await agent("GET", "/config"); cfgAt = Date.now(); }
      const { items } = await agent("GET", "/queue?n=5");
      if (!items.length) { await set({ state: "idle (nothing due)" }); await sleep(3000); continue; }
      const tab = await feederTab();
      for (const it of items) {
        const res = await chrome.tabs.sendMessage(tab, { type: "mutfeeder:fetch", uid: it.uid, platform: cfg.platform });
        if (res && res.status === 200 && res.data) {
          await agent("POST", "/ingest", { uid: it.uid, data: res.data });
          await bump("checks");
          await set({ state: "running" });
          backoff = 0;
        } else if (res && BLOCK_CODES.has(res.status)) {
          backoff = Math.min(900000, Math.max(60000, backoff * 2));
          await bump("errors");
          await set({ state: `mut.gg refused (${res.status}), pausing ${backoff / 1000}s` });
          await agent("POST", "/status", { state: "blocked", detail: `${res.status} ${res.detail || ""}` }).catch(() => {});
          await sleep(backoff);
          await agent("POST", "/status", { state: "ok" }).catch(() => {});
          break;
        }
        await sleep(cfg.interval_ms);
      }
    }
    await set({ state: "stopped" });
  } catch (e) {
    await set({ state: `error: ${String(e).slice(0, 80)}` });
    await bump("errors");
  } finally {
    running = false;
  }
}

// The service worker can be suspended; this alarm restarts the loop if so.
chrome.alarms.create("mutfeeder", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => loop());
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg && msg.type === "mutfeeder:kick") { loop(); reply({ ok: true }); }
});
