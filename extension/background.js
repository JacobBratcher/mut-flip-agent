// Drives the feeder: asks the agent which cards are due, has the mut.gg tab fetch them,
// and posts each result straight to the agent (which alerts Discord immediately).
const FEEDER_URL = "https://www.mut.gg/price-tracker/";
const BLOCK_CODES = new Set([0, 403, 429, 503]);
let running = false;

const STALL_MS = 5 * 60 * 1000;   // no progress this long = wedged, reload the extension
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let beat = Date.now();             // last time the loop made progress
const alive = () => { beat = Date.now(); };

// Nothing may wait forever: a hung promise used to freeze the feeder until someone restarted it.
function withTimeout(p, ms, what) {
  let t;
  return Promise.race([
    p,
    new Promise((_, rej) => { t = setTimeout(() => rej(new Error(`${what} timed out`)), ms); }),
  ]).finally(() => clearTimeout(t));
}
const tabMsg = (tabId, msg, ms) => withTimeout(chrome.tabs.sendMessage(tabId, msg), ms, msg.type);
const get = (keys) => chrome.storage.local.get(keys);
const set = (obj) => chrome.storage.local.set(obj);

// Settings written by install.ps1 (config.json) win, so re-running the installer updates them.
async function bootstrap() {
  try {
    const r = await fetch(chrome.runtime.getURL("config.json"));
    if (!r.ok) return;
    const c = await r.json();
    const cur = await get(["agentUrl", "token", "enabled", "workerId"]);
    const patch = {};
    if (c.agentUrl && c.agentUrl !== cur.agentUrl) patch.agentUrl = c.agentUrl;
    if (c.token && c.token !== cur.token) patch.token = c.token;
    const workerId = /^[a-zA-Z0-9_-]{1,40}$/.test(c.workerId || "") ? c.workerId : "primary";
    if (cur.workerId !== workerId) patch.workerId = workerId;
    if (cur.enabled === undefined && c.autostart) patch.enabled = true;
    if (Object.keys(patch).length) await set(patch);
  } catch (_) { /* no config.json: configured via the Settings page instead */ }
}

async function agent(method, path, body) {
  const { agentUrl, token, workerId = "primary" } = await get(["agentUrl", "token", "workerId"]);
  const r = await fetch(agentUrl.replace(/\/$/, "") + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Feeder-Token": token || "",
               "X-Feeder-Version": chrome.runtime.getManifest().version,
               "X-Feeder-Id": workerId },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15000),
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

const MUT_TABS = ["https://www.mut.gg/*", "https://mut.gg/*"];
const isMut = (u) => /^https:\/\/(www\.)?mut\.gg\//.test(u || "");

async function waitForPing(tabId) {
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    try { await tabMsg(tabId, { type: "mutfeeder:ping" }, 3000); return true; } catch (_) { /* loading */ }
  }
  return false;
}

async function findOrOpenTab() {
  let { tabId } = await get(["tabId"]);
  const open = await chrome.tabs.query({ url: MUT_TABS });
  if (!open.some((t) => t.id === tabId)) tabId = open.length ? open[0].id : null;
  // Keep exactly one feeder tab (stalls used to leave a new pinned tab behind each time).
  const extra = open.filter((t) => t.id !== tabId).map((t) => t.id);
  if (extra.length) await chrome.tabs.remove(extra).catch(() => {});
  if (tabId) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (isMut(t.url)) {
        await set({ tabId });
        try { await tabMsg(tabId, { type: "mutfeeder:ping" }, 3000); return tabId; } catch (_) { }
        // Tab exists but its page stopped answering: reload it in place.
        await chrome.tabs.update(tabId, { url: FEEDER_URL, pinned: true });
        if (await waitForPing(tabId)) return tabId;
        await chrome.tabs.remove(tabId).catch(() => {});
      }
    } catch (_) { /* tab gone */ }
  }
  const t = await chrome.tabs.create({ url: FEEDER_URL, pinned: true, active: false });
  await set({ tabId: t.id });
  if (await waitForPing(t.id)) return t.id;
  throw new Error("mut.gg tab did not load");
}

let statsLock = Promise.resolve();
function bump(field, n = 1) {
  const update = statsLock.then(async () => {
    const { stats = {} } = await get(["stats"]);
    const today = new Date().toDateString();
    if (stats.day !== today) Object.assign(stats, { day: today, checks: 0, errors: 0, requests: 0 });
    stats[field] = (stats[field] || 0) + n;
    stats.last = Date.now();
    await set({ stats });
  });
  statsLock = update.catch(() => {});
  return update;
}

// A few cards can refresh concurrently; every HTTP attempt uses the agent's shared gate.
const MAX_INFLIGHT = 2;
const UPDATE_WAITS = [1500, 2000, 4000];

// Queue permit waiters within one profile. Without FIFO ordering, its concurrent
// checks race for each slot and an unlucky leased card can expire unscanned.
let permitLock = Promise.resolve();
function requestPermit(deadline) {
  const turn = permitLock.then(async () => {
    while (true) {
      if (!(await get(["enabled"])).enabled || Date.now() >= deadline) return { kind: "pending" };
      const permit = await agent("POST", "/request-permit", {});
      alive();
      await set({ requestRate: permit.rpm });
      if (permit.blocked) return { kind: "blocked", res: { status: 429, detail: "shared cooldown" },
                                   wait_ms: permit.wait_ms };
      if (permit.allowed) return { kind: "permit" };
      await sleep(Math.min(10000, Math.max(1, permit.wait_ms)));
    }
  });
  permitLock = turn.catch(() => {});
  return turn;
}

async function checkOne(tab, it, cfg) {
  let res;
  // Bound each refresh so it finishes within the server's five-minute card lease.
  const deadline = Date.now() + 85000;
  const retryLimit = Number.isInteger(it.refresh_retries)
    ? Math.max(0, Math.min(UPDATE_WAITS.length, it.refresh_retries)) : UPDATE_WAITS.length;
  for (let attempt = 0; attempt <= retryLimit; attempt++) {
    const gate = await requestPermit(deadline);
    if (gate.kind !== "permit") return gate;
    try {
      res = await tabMsg(tab, { type: "mutfeeder:fetch", uid: it.uid, platform: cfg.platform }, 25000);
    } catch (e) {
      return { kind: "hang", error: e };
    }
    alive();
    await bump("requests");
    const feedback = await agent("POST", "/request-result", {
      status: Math.max(0, res?.status || 0), retry_after: res?.retry_after,
      uid: it.uid, refreshing: res?.data?.updating === true,
      retry_exhausted: res?.data?.updating === true && attempt === retryLimit,
      outcome: res?.outcome === "api_error" ? "api_error" : res?.status === -1 ? "timeout" : res?.status === 0 ? "network_error" : "http",
    });
    if (res && res.status !== 200) {
      // Allowlisted metadata only: no response bodies, URLs, cookies or tokens.
      await set({ lastFailure: { at: Date.now(), uid: it.uid, status: res.status,
        httpStatus: res.http_status || 0,
        responseKind: ["json", "html", "other"].includes(res.response_kind) ? res.response_kind : "unknown",
        challenged: res.challenged === true, redirected: res.redirected === true } });
    }
    if (res && BLOCK_CODES.has(res.status)) return { kind: "blocked", res,
      wait_ms: Math.max(60000, feedback.wait_ms) };
    if (!res || res.status !== 200 || !res.data?.updating) break;
    // Never ingest an unfinished refresh as a successful fresh check.
    if (attempt === retryLimit) return { kind: "pending" };
    await sleep(UPDATE_WAITS[attempt]);
  }
  if (res && res.status === 200 && res.data) {
    const accepted = await agent("POST", "/ingest", { uid: it.uid, data: res.data });
    if (accepted.ok) await bump("checks");
    await set({ state: "running" });
    return { kind: "ok" };
  }
  if (res && res.status === -1) return { kind: "timeout" };
  return { kind: "other" };
}

async function loop() {
  if (running) return;
  running = true;
  alive();
  try {
    await bootstrap();
    const { agentUrl, token } = await get(["agentUrl", "token"]);
    if (!agentUrl || !token) { await set({ state: "not configured" }); return; }
    let cfg = await agent("GET", "/config");
    if (cfg.request_budget_version !== 1) throw new Error("Update MUT Flip Agent to 1.9.0 before running this feeder");
    let cfgAt = Date.now();
    while ((await get(["enabled"])).enabled) {
      if (Date.now() - cfgAt > 600000) { cfg = await agent("GET", "/config"); cfgAt = Date.now(); }
      alive();
      const tab = await withTimeout(feederTab(), 90000, "opening the mut.gg tab");

      let stop = null;
      let hadItems = false;
      const worker = async () => {
        while (!stop && (await get(["enabled"])).enabled) {
          // Lease just in time; a slow card no longer holds up a batch of fast cards.
          const { items } = await agent("GET", "/queue?n=1");
          if (!items.length || stop) return;
          hadItems = true;
          const r = await checkOne(tab, items[0], cfg);
          if (!["ok", "other", "pending"].includes(r.kind) && !stop) stop = r;
        }
      };
      // Settle every worker before another loop can start, including agent API errors.
      const results = await Promise.allSettled(Array.from({ length: MAX_INFLIGHT }, async () => {
        try { await worker(); } catch (error) { stop = stop || { kind: "error" }; throw error; }
      }));
      const failed = results.find((r) => r.status === "rejected");
      if (failed) throw failed.reason;
      if (!hadItems) { await set({ state: "idle (nothing due)" }); await sleep(3000); continue; }

      if (!stop) { await set({ state: "running" }); continue; }
      if (stop.kind === "hang") {
        // The page stopped answering: reload it now, and let the alarm restart the loop.
        await chrome.tabs.reload(tab).catch(() => {});
        throw stop.error;
      }
      if (stop.kind === "timeout") {
        // mut.gg didn't answer in time: refresh the page and carry on shortly.
        await bump("errors");
        await set({ state: "mut.gg timed out, retrying" });
        await chrome.tabs.reload(tab).catch(() => {});
        await sleep(5000);
        continue;
      }
      // blocked
      const res = stop.res;
      const backoff = Math.max(60000, stop.wait_ms || 0);
      await bump("errors");
      await set({ state: `mut.gg refused (${res.status}), pausing ${backoff / 1000}s` });
      await agent("POST", "/status", { state: "blocked", detail: `${res.status} ${res.detail || ""}` }).catch(() => {});
      beat = Date.now() + backoff;   // a deliberate pause is not a stall
      const resumeAt = Date.now() + backoff;
      while (Date.now() < resumeAt && (await get(["enabled"])).enabled) {
        await sleep(Math.min(30000, resumeAt - Date.now()));
        alive();
      }
      alive();
      await agent("POST", "/status", { state: "ok" }).catch(() => {});
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
// If the loop has made no progress for STALL_MS it is stuck on something we can't cancel,
// so reload the whole extension (a fresh service worker and a clean start).
chrome.alarms.onAlarm.addListener(async () => {
  if (running && Date.now() - beat > STALL_MS) {
    await set({ state: "stalled, restarting" }).catch(() => {});
    chrome.runtime.reload();
    return;
  }
  loop();
});
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg && msg.type === "mutfeeder:kick") { loop(); reply({ ok: true }); }
});
