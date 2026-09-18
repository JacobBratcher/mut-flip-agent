// Runs on mut.gg pages. Fetches a card's prices exactly like mut.gg's own page does
// (same URL, same origin, your normal session), including its "still updating" re-checks.
const UPDATE_WAITS = [2000, 5000, 10000];

async function fetchPrices(uid, platform) {
  const path = `/api/mutdb/prices/${uid}/${platform}/`;
  let last = null;
  for (let i = 0; i <= UPDATE_WAITS.length; i++) {
    const r = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    const type = r.headers.get("content-type") || "";
    if (!r.ok || !type.includes("json")) {
      return { status: r.status || 0, detail: (await r.text()).slice(0, 120) };
    }
    const body = await r.json();
    last = body && body.data;
    if (!last || !last.updating || i === UPDATE_WAITS.length) break;
    await new Promise((res) => setTimeout(res, UPDATE_WAITS[i]));
  }
  return { status: 200, data: last };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "mutfeeder:fetch") {
    fetchPrices(msg.uid, msg.platform)
      .then(reply)
      .catch((e) => reply({ status: 0, detail: String(e).slice(0, 120) }));
    return true; // async reply
  }
  if (msg && msg.type === "mutfeeder:ping") {
    reply({ ok: true });
  }
});
