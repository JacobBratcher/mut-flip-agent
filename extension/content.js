// Runs on mut.gg pages. Fetches a card's prices exactly like mut.gg's own page does
// (same URL, same origin, your normal session). Exactly ONE HTTP request per message:
// the background worker obtains a shared budget permit before every attempt/re-check.

async function fetchPrices(uid, platform) {
  const path = `/api/mutdb/prices/${uid}/${platform}/`;
  const r = await fetch(path, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" },
                                signal: AbortSignal.timeout(20000) });
  const type = r.headers.get("content-type") || "";
  if (!r.ok || !type.includes("json")) {
    // Keep enough metadata to distinguish a rejected API call from a successful
    // HTTP response containing a challenge/login page. Never retain page bodies.
    const response_kind = type.includes("json") ? "json" : type.includes("html") ? "html" : "other";
    const challenged = r.headers.get("cf-mitigated") === "challenge";
    return { status: r.ok ? 403 : r.status, retry_after: r.headers.get("Retry-After"),
             http_status: r.status, response_kind, challenged, redirected: !!r.redirected,
             detail: `HTTP ${r.status} (${response_kind}${challenged ? ", challenge" : ""})` };
  }
  const body = await r.json();
  const data = body && body.data;
  const apiError = body?.error || (Array.isArray(body?.errors) ? body.errors.length : body?.errors);
  if (apiError || !data || typeof data !== "object" || Array.isArray(data) ||
      (!data.updating && (!data.pricesData || typeof data.pricesData !== "object" || Array.isArray(data.pricesData)))) {
    // A successful HTTP response can still contain an API error. Keep its body private.
    return { status: 0, outcome: "api_error", http_status: 200, response_kind: "json" };
  }
  return { status: 200, data };
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
