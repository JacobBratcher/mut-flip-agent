// Runs on mut.gg pages. Fetches a card's prices exactly like mut.gg's own page does
// (same URL, same origin, your normal session). Exactly ONE HTTP request per message:
// the background worker obtains a shared budget permit before every attempt/re-check.

async function fetchPrices(uid, platform) {
  const path = `/api/mutdb/prices/${uid}/${platform}/`;
  const r = await fetch(path, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
                                signal: AbortSignal.timeout(20000) });
  const type = r.headers.get("content-type") || "";
  if (!r.ok || !type.includes("json")) {
    return { status: r.ok ? 403 : r.status, retry_after: r.headers.get("Retry-After"),
             detail: (await r.text()).slice(0, 120) };
  }
  const body = await r.json();
  return { status: 200, data: body && body.data };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "mutfeeder:fetch") {
    fetchPrices(msg.uid, msg.platform)
      .then(reply)
      // A timeout is a slow/hung request, not mut.gg refusing us: report it separately.
      .catch((e) => reply({ status: e && e.name === "TimeoutError" ? -1 : 0, detail: String(e).slice(0, 120) }));
    return true; // async reply
  }
  if (msg && msg.type === "mutfeeder:ping") {
    reply({ ok: true });
  }
});
